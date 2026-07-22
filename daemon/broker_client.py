"""Daemon-side client for the PTY broker. Spawns ONE broker subprocess (at daemon boot,
before threads) and delegates PTY spawns to it. Serializes requests behind one lock and
lazily respawns the broker once if it has died (existing sessions are unaffected — the
daemon already owns their master fds)."""
import os
import subprocess
import sys
import threading

from daemon.broker_proto import send_msg, recv_msg, make_socketpair


class BrokerSpawnError(Exception):
    def __init__(self, stage, err_errno):
        super().__init__(f"broker spawn_failed at {stage} (errno={err_errno})")
        self.stage = stage
        self.err_errno = err_errno


# Upper bound on how long the daemon waits for the broker's reply to one spawn. Generous, because
# the broker forks, opens a PTY and execs a real CLI under whatever load the machine is under —
# this is a deadlock backstop, not a latency budget. A spawn that genuinely needs longer than this
# has already failed in a way the caller must hear about.
_SPAWN_REPLY_TIMEOUT = 30.0


class BrokerClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._sock = None
        self._proc = None
        self._start()

    def _start(self):
        daemon_end, broker_end = make_socketpair()
        # A recv deadline, because peer-close is NOT a portable liveness signal. Closing one end
        # of an AF_UNIX/SOCK_DGRAM pair wakes the other end's recvmsg with ECONNRESET on macOS
        # (broker_proto turns that into EOFError); Linux delivers no wakeup at all and the recv
        # never returns. spawn() blocks in recv while holding self._lock, so on Linux one dead
        # broker would wedge every later spawn behind it. The deadline needs no new handling:
        # socket.timeout IS TimeoutError, an OSError, so spawn()'s existing
        # `except (OSError, EOFError)` already routes it to restart-and-retry.
        daemon_end.settimeout(_SPAWN_REPLY_TIMEOUT)
        os.set_inheritable(broker_end.fileno(), True)
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "daemon.pty_broker", str(broker_end.fileno())],
            pass_fds=[broker_end.fileno()],          # NOT start_new_session (no controlling-tty grab)
            close_fds=True,
        )
        broker_end.close()                            # broker holds the other end
        self._sock = daemon_end

    def _alive(self):
        return self._proc is not None and self._proc.poll() is None

    def spawn(self, argv, cwd, env, cols, rows):
        with self._lock:
            if not self._alive():
                self._restart_locked()
            req = {"v": 1, "argv": list(argv), "cwd": cwd,
                   "env": dict(env), "cols": cols, "rows": rows}
            try:
                send_msg(self._sock, req)
                resp, master = recv_msg(self._sock)
            except (OSError, EOFError):
                self._restart_locked()                 # one transparent retry after a mid-call death
                send_msg(self._sock, req)
                resp, master = recv_msg(self._sock)
        if resp.get("status") == "ok" and master is None:
            raise BrokerSpawnError("missing_fd", None)     # protocol violation: ok but no master
        if resp.get("status") != "ok":
            if master is not None:
                os.close(master)
            raise BrokerSpawnError(resp.get("stage"), resp.get("errno"))
        os.set_inheritable(master, False)                  # SCM_RIGHTS fds aren't CLOEXEC by default
        return master, resp["pid"], resp["pgid"]

    def _restart_locked(self):
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait(timeout=5)
        self._start()

    def close(self):
        with self._lock:
            try:
                if self._sock is not None:
                    self._sock.close()                 # EOF -> broker exits
            except OSError:
                pass
            self._sock = None
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()


_broker = None


def set_broker(client):
    global _broker
    _broker = client


def get_broker():
    if _broker is None:
        raise RuntimeError("broker not initialized (set_broker must run at daemon boot)")
    return _broker
