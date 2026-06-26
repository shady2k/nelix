"""Single-threaded PTY spawn broker. The daemon spawns ONE of these at boot, BEFORE any
threads exist, and delegates every PTY fork to it over an inherited AF_UNIX/SOCK_DGRAM
socketpair. Because this process has exactly one thread, fork-under-threads is impossible.

stdlib-only; imports NO app modules (except the stdlib-only daemon.broker_proto). Run as:
    python -m daemon.pty_broker <inherited_socket_fd>
"""
import fcntl
import json
import os
import select
import signal
import socket
import struct
import sys
import termios

from daemon.broker_proto import send_msg, recv_msg

_READ_TIMEOUT = 10.0          # bound every pipe read; a stuck child must not wedge the broker
_MAIN_POLL = 0.5              # socket-recv wake interval: poll parent liveness + detect peer
                              # close (macOS: closing a SOCK_DGRAM peer does NOT wake a blocked
                              # recvmsg/select, so we re-check on each timeout instead)


def _set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def _close_fds_except(keep):
    keep = set(keep)
    try:
        maxfd = os.sysconf("SC_OPEN_MAX")
    except (ValueError, OSError):
        maxfd = 4096
    if maxfd < 0 or maxfd > 65536:
        maxfd = 65536
    for fd in range(3, maxfd):
        if fd in keep:
            continue
        try:
            os.close(fd)
        except OSError:
            pass


def _child_exec(slave_path, rows, cols, cwd, argv, env, err_w):
    # Runs in the FINAL child F. On any failure: write {stage,errno} to err_w and _exit(127).
    # err_w is CLOEXEC -> a successful execvpe closes it, which the broker reads as EOF=success.
    stage = "open_slave"
    try:
        slave = os.open(slave_path, os.O_RDWR)        # after-setsid open happens inside login_tty
        stage = "login_tty"
        os.login_tty(slave)                           # setsid + TIOCSCTTY + dup2(0,1,2) + close
        _set_winsize(0, rows, cols)
        stage = "chdir"
        if cwd:
            os.chdir(cwd)
        stage = "closefds"
        _close_fds_except({0, 1, 2, err_w})
        stage = "exec"
        os.execvpe(argv[0], argv, env)
    except Exception as e:
        try:
            os.write(err_w, json.dumps({"stage": stage,
                                        "errno": getattr(e, "errno", None)}).encode())
        except OSError:
            pass
    os._exit(127)


def _read_pid(pid_r):
    r, _, _ = select.select([pid_r], [], [], _READ_TIMEOUT)
    if not r:
        return None
    data = os.read(pid_r, 64)
    try:
        return int(data) if data else None
    except ValueError:
        return None


def _read_err(err_r):
    # EOF (b"") => success; data => failure dict; timeout => {"stage":"spawn_timeout"}.
    r, _, _ = select.select([err_r], [], [], _READ_TIMEOUT)
    if not r:
        return {"stage": "spawn_timeout", "errno": None}
    data = os.read(err_r, 256)
    if not data:
        return None
    try:
        return json.loads(data.decode())
    except ValueError:
        return {"stage": "unknown", "errno": None}


def handle_spawn(req):
    argv = list(req.get("argv") or [])
    cwd = req.get("cwd")
    env = dict(req.get("env") or {})
    cols = int(req.get("cols", 120))
    rows = int(req.get("rows", 40))
    if not argv:
        return {"v": 1, "status": "spawn_failed", "stage": "argv", "errno": None}, None
    try:
        master, slave = os.openpty()              # both non-inheritable (CLOEXEC) by default
    except OSError as e:
        return {"v": 1, "status": "spawn_failed", "stage": "openpty", "errno": e.errno}, None
    _set_winsize(master, rows, cols)
    slave_path = os.ttyname(slave)
    os.close(slave)                               # keep only the path + master

    pid_r, pid_w = os.pipe()
    err_r, err_w = os.pipe()                       # both CLOEXEC by default (os.pipe, PEP 446)

    intermediate = os.fork()
    if intermediate == 0:                          # ---- intermediate I ----
        os.close(master); os.close(pid_r); os.close(err_r)
        final = os.fork()
        if final == 0:                             # ---- final F (executor) ----
            os.close(pid_w)
            _child_exec(slave_path, rows, cols, cwd, argv, env, err_w)
            os._exit(127)                          # unreachable
        try:
            os.write(pid_w, str(final).encode())   # report grandchild pid, then vanish
        except OSError:
            pass
        os._exit(0)

    # ---- broker parent ----
    os.close(pid_w); os.close(err_w)               # broker holds only the READ ends
    os.waitpid(intermediate, 0)                    # reap I now -> no zombie; F reparents to init
    pid = _read_pid(pid_r); os.close(pid_r)
    err = _read_err(err_r); os.close(err_r)

    if pid is None:
        os.close(master)
        return {"v": 1, "status": "spawn_failed", "stage": "intermediate", "errno": None}, None
    if err is not None:
        os.close(master)
        if err.get("stage") == "spawn_timeout":
            # On timeout F is still pre-exec: it may not have reached setsid() yet, so its pgid
            # is not guaranteed to equal `pid` and killpg(pid) could hit an unrelated group.
            # Pre-exec F has no child subtree, so kill the single process by pid, not its group.
            try:
                os.kill(pid, signal.SIGKILL)       # F known but never exec'd: don't leak it
            except OSError:
                pass
        return {"v": 1, "status": "spawn_failed", **err}, None
    return {"v": 1, "status": "ok", "pid": pid, "pgid": pid}, master


def main():
    sock = socket.fromfd(int(sys.argv[1]), socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.settimeout(_MAIN_POLL)                       # wake periodically (see _MAIN_POLL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)     # do not inherit daemon handlers
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    daemon_pid = os.getppid()                         # the daemon that spawned us (direct parent)
    while True:
        try:
            req, _fd = recv_msg(sock)
        except TimeoutError:                          # idle wake: peer-close (re-checked next loop)
            if os.getppid() != daemon_pid:            # daemon gone (reparented) -> exit, no leak
                return
            continue
        except EOFError:
            return                                     # daemon closed its end -> clean exit
        except ValueError:                            # malformed/oversized datagram (incl.
            continue                                   # JSONDecodeError): drop it, keep serving
        except OSError:
            return
        resp, master = handle_spawn(req)
        try:
            send_msg(sock, resp, fd=master)
        except OSError:
            pass
        finally:
            if master is not None:
                os.close(master)                       # broker drops its copy after handing it off


if __name__ == "__main__":
    main()
