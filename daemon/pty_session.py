import os
import select
import time

from daemon.errors import PtyWriteTimeout
from daemon.renderer.base import make_renderer


_ESU = b"\x1b[?2026l"


def _esu_partial_len(buf):
    # longest k in 1..len(_ESU)-1 such that buf ends with a proper prefix of the ESU sequence,
    # so a sequence split across reads is held back (not fed early, not missed by the detector).
    for k in range(len(_ESU) - 1, 0, -1):
        if buf.endswith(_ESU[:k]):
            return k
    return 0


def render_raw(data, cols=120, rows=40):
    """Replay raw PTY bytes through a fresh renderer and return what render() would show.
    Pure: no child, no dialog. The capture tool and the daemon share make_renderer, so offline
    and live rendering can never drift."""
    r = make_renderer(cols, rows)
    try:
        r.feed(data if isinstance(data, (bytes, bytearray)) else bytes(data))
        return r.render()
    finally:
        r.close()


class PtySession:
    def __init__(self, master_fd, pid, pgid, cols=120, rows=40, dialog=None, transcript=None):
        self._fd = master_fd
        self._pid = pid
        self._pgid = pgid
        self._cols = cols
        self._rows = rows
        self._dialog = dialog
        self._transcript = transcript
        self._eof_seen = False
        self._renderer = make_renderer(cols, rows)
        self._esu_carry = b""

    def pump(self, timeout=0.1):
        if self._fd is None or self._eof_seen:
            return False
        try:
            r, _, _ = select.select([self._fd], [], [], timeout)
        except (OSError, ValueError):
            self._eof_seen = True
            return False
        if not r:
            return False
        try:
            data = os.read(self._fd, 65536)
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:                 # EIO on slave hangup, or fd torn down
            self._eof_seen = True
            return False
        if not data:                    # EOF: all slave writers closed (child gone)
            self._eof_seen = True
            return False
        self._feed(data)
        return True

    def _feed(self, data):
        # Tee the whole chunk to raw; advance the renderer. With a transcript sink, segment the
        # stream at each DEC-2026 ESU and snapshot AT that frame boundary (a single read holds many
        # frames), holding back a trailing partial ESU across calls.
        if self._dialog is not None:
            self._dialog.append_raw(data)
        if self._transcript is None:
            self._renderer.feed(data)
            return
        buf = self._esu_carry + data
        self._esu_carry = b""
        pos = 0
        while True:
            i = buf.find(_ESU, pos)
            if i < 0:
                break
            end = i + len(_ESU)
            self._renderer.feed(buf[pos:end])
            self._transcript.observe(self._renderer.snapshot())
            pos = end
        rest = buf[pos:]
        k = _esu_partial_len(rest)
        if k:
            self._esu_carry = rest[len(rest) - k:]
            rest = rest[:len(rest) - k]
        if rest:
            self._renderer.feed(rest)

    def finalize(self):
        # Commit the stable visible tail at a stop (replaces the Phase-1 flush_viewport).
        if self._transcript is not None:
            self._transcript.finalize(self._renderer.snapshot())

    def render(self):
        return self._renderer.render()

    def write(self, data, timeout=None, drain_output=False):
        # Non-blocking, deadline-bounded write. A blocking ptyprocess.write() would wedge
        # the monitor thread forever if the child stops draining its stdin (PTY input
        # buffer full) — and on macOS select-for-write on a PTY master can report writable
        # even when the buffer is full, so we set the fd non-blocking: os.write raises
        # BlockingIOError instead of blocking. With `timeout` set, raise PtyWriteTimeout if
        # `data` is not fully written in time. Only the monitor thread writes, so toggling
        # the fd's blocking mode here (restored in finally) is safe vs the read path.
        # With drain_output, also consume the child's output while writing (see below) —
        # opt-in because a concurrent reader (the monitor's pump) must not race the screen;
        # delivery passes it because there the monitor itself owns both the write and the read.
        if self._fd is None or self._eof_seen:
            return
        b = data.encode()
        fd = self._fd
        mv = memoryview(b)
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            old_blocking = os.get_blocking(fd)
            os.set_blocking(fd, False)
        except (OSError, ValueError):
            return                          # fd already closed (e.g. concurrent stop())
        try:
            while mv:
                if deadline is not None and time.monotonic() >= deadline:
                    raise PtyWriteTimeout(len(b) - len(mv), len(b))
                try:
                    n = os.write(fd, mv[:65536])
                    if n <= 0:              # no progress: treat as "would block", wait
                        raise BlockingIOError()
                    mv = mv[n:]
                    continue
                except BlockingIOError:
                    pass                    # buffer full: wait (bounded) for space
                except (OSError, ValueError):
                    return                  # fd closed / child gone
                # Buffer full: wait (bounded) for the slave to accept more. With drain_output,
                # also watch for readable output and consume it: a TUI that echoes/re-renders
                # the input fills the PTY output buffer, then blocks writing it (nobody reads
                # the master) and so stops reading our input — a flow-control deadlock that
                # would otherwise eat the whole budget and fail an in-flight large write.
                # Reading here keeps the child draining so the write can complete.
                wait = 0.1 if deadline is None else min(0.1, max(0.0, deadline - time.monotonic()))
                try:
                    r, _, _ = select.select([fd] if drain_output else [], [fd], [], wait)
                except (OSError, ValueError):
                    return
                if r:
                    try:
                        chunk = os.read(fd, 65536)
                    except (BlockingIOError, OSError, ValueError):
                        chunk = b""
                    if chunk:
                        self._feed(chunk)
                    else:
                        self._eof_seen = True
                        return
        finally:
            try:
                os.set_blocking(fd, old_blocking)
            except OSError:
                pass

    def is_alive(self):
        if self._fd is None or self._eof_seen:
            return False
        try:
            os.kill(self._pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            pass
        try:
            return os.getpgid(self._pid) == self._pgid
        except OSError:
            return False

    def exit_code(self):
        return None                     # no waitpid for a non-child; status unavailable

    def leader_pid(self):
        return self._pid

    def leader_pgid(self):
        try:
            return os.getpgid(self._pid)
        except OSError:
            return None

    def assert_leader_is_group_leader(self):
        """The reaper kills by process GROUP; that only reaps the whole subtree if the PTY
        child is its own group leader (setsid -> pid == pgid). Fail loudly if not."""
        pid, pgid = self.leader_pid(), self.leader_pgid()
        if pid is None or pid != pgid:
            raise RuntimeError(f"pty leader {pid} is not its own group leader (pgid={pgid})")

    def leader_status(self):
        from daemon.launchers.base import LeaderStatus
        return LeaderStatus(alive=self.is_alive(), exit_code=None, signal=None,
                            status_available=False)

    def close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self._renderer.close()
