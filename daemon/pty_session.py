import os
import select
import time

import pyte
import ptyprocess

from daemon.errors import PtyWriteTimeout


def _row_text(row):
    # pyte history rows are {col: Char}; display rows are already strings.
    if isinstance(row, str):
        return row.rstrip()
    return "".join(row[c].data for c in sorted(row)).rstrip()


class PtySession:
    def __init__(self, argv, cwd=None, cols=120, rows=40, env=None, dialog=None):
        self._argv = argv
        self._cwd = cwd
        self._cols = cols
        self._rows = rows
        self._env = env
        self._child = None
        self._dialog = dialog
        self._screen = pyte.HistoryScreen(cols, rows, history=100000, ratio=0.5)
        self._stream = pyte.ByteStream(self._screen)
        self._history_committed = 0   # how many top-history lines already added to the dialog

    def spawn(self):
        self._child = ptyprocess.PtyProcess.spawn(
            self._argv, dimensions=(self._rows, self._cols), cwd=self._cwd, env=self._env)

    def pump(self, timeout=0.1):
        if self._child is None:
            return False
        try:
            r, _, _ = select.select([self._child.fd], [], [], timeout)
        except (OSError, ValueError):
            return False  # fd closed (e.g. child exited or session torn down)
        if not r:
            return False
        try:
            data = self._child.read(65536)
        except (EOFError, OSError, ValueError):
            return False
        if self._dialog is not None:
            self._dialog.append_raw(data)
        self._stream.feed(data)
        self._commit_scrolled()
        return True

    def _commit_scrolled(self):
        # Lines that scrolled off the top of the normal buffer are final — commit each once.
        # Inert for alt-screen/repaint-only TUIs (history stays empty — Spike D); those rely on
        # flush_viewport() at each stop instead.
        top = list(self._screen.history.top)
        if self._dialog is not None and len(top) > self._history_committed:
            for row in top[self._history_committed:]:
                self._dialog.add_line(_row_text(row))
            self._history_committed = len(top)

    def flush_viewport(self, dialog):
        # Commit the current viewport's non-empty lines (final turn tail / fallback snapshot).
        for line in self._screen.display:
            t = line.rstrip()
            if t:
                dialog.add_line(t)

    def render(self):
        return "\n".join(self._screen.display)

    def write(self, data, timeout=None):
        # Non-blocking, deadline-bounded write. A blocking ptyprocess.write() would wedge
        # the monitor thread forever if the child stops draining its stdin (PTY input
        # buffer full) — and on macOS select-for-write on a PTY master can report writable
        # even when the buffer is full, so we set the fd non-blocking: os.write raises
        # BlockingIOError instead of blocking. With `timeout` set, raise PtyWriteTimeout if
        # `data` is not fully written in time. Only the monitor thread writes, so toggling
        # the fd's blocking mode here (restored in finally) is safe vs the read path.
        if self._child is None:
            return
        b = data.encode()
        fd = self._child.fd
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
                wait = 0.1 if deadline is None else min(0.1, max(0.0, deadline - time.monotonic()))
                try:
                    select.select([], [fd], [], wait)
                except (OSError, ValueError):
                    return
        finally:
            try:
                os.set_blocking(fd, old_blocking)
            except OSError:
                pass

    def is_alive(self):
        return self._child is not None and self._child.isalive()

    def exit_code(self):
        if self._child is None:
            return None
        return self._child.exitstatus

    def leader_pid(self):
        return self._child.pid if self._child is not None else None

    def leader_pgid(self):
        if self._child is None:
            return None
        try:
            return os.getpgid(self._child.pid)
        except OSError:
            return None

    def leader_status(self):
        from daemon.launchers.base import LeaderStatus
        if self._child is None:
            return LeaderStatus(alive=False, exit_code=None, signal=None, status_available=False)
        alive = self._child.isalive()           # nonblocking reap; populates exit/signal status
        if alive:
            return LeaderStatus(alive=True, exit_code=None, signal=None, status_available=False)
        ec, sig = self._child.exitstatus, self._child.signalstatus
        return LeaderStatus(alive=False, exit_code=ec, signal=sig,
                            status_available=(ec is not None or sig is not None))

    def close(self):
        if self._child is not None:
            try:
                self._child.close(force=True)
            except OSError:
                pass            # EIO etc. on a torn-down PTY: closing a broken fd is fine
