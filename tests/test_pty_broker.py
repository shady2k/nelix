import os
import signal
import subprocess
import sys
import time

import daemon.pty_broker as pty_broker
from daemon.broker_proto import send_msg, recv_msg, make_socketpair


def _start_broker():
    daemon_end, broker_end = make_socketpair()
    os.set_inheritable(broker_end.fileno(), True)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen(
        [sys.executable, "-m", "daemon.pty_broker", str(broker_end.fileno())],
        pass_fds=[broker_end.fileno()], cwd=repo,
        env={**os.environ, "PYTHONPATH": repo},
    )
    broker_end.close()
    return proc, daemon_end


def test_spawn_cat_returns_master_pid_pgid(tmp_path):
    proc, sock = _start_broker()
    try:
        send_msg(sock, {"v": 1, "argv": ["cat"], "cwd": str(tmp_path),
                        "env": dict(os.environ), "cols": 80, "rows": 24})
        resp, master = recv_msg(sock)
        assert resp["status"] == "ok"
        assert master is not None
        pid, pgid = resp["pid"], resp["pgid"]
        assert pid == pgid                                   # setsid -> own group leader
        assert os.getpgid(pid) == pgid
        os.write(master, b"hi\n")
        time.sleep(0.3)
        assert b"hi" in os.read(master, 4096)               # the master really drives the child
        os.close(master)
        os.kill(pid, 9)
    finally:
        sock.close(); proc.terminate(); proc.wait(timeout=5)


def test_exec_failure_is_clean_spawn_failed(tmp_path):
    proc, sock = _start_broker()
    try:
        send_msg(sock, {"v": 1, "argv": ["/nonexistent/definitely-not-a-binary"],
                        "cwd": str(tmp_path), "env": dict(os.environ), "cols": 80, "rows": 24})
        resp, master = recv_msg(sock)
        assert resp["status"] == "spawn_failed"
        assert master is None
        assert resp.get("stage") in ("exec", "open_slave", "chdir")
    finally:
        sock.close(); proc.terminate(); proc.wait(timeout=5)


def test_broker_exits_on_socketpair_eof():
    proc, sock = _start_broker()
    sock.close()                                            # daemon end gone -> broker should exit
    proc.wait(timeout=5)
    assert proc.returncode == 0


def test_child_has_controlling_tty(tmp_path):
    # Prove login_tty handed the child the slave as its controlling terminal: the foreground
    # process group of the master's terminal must be the child's own (setsid) group == pid.
    proc, sock = _start_broker()
    try:
        send_msg(sock, {"v": 1, "argv": ["cat"], "cwd": str(tmp_path),
                        "env": dict(os.environ), "cols": 80, "rows": 24})
        resp, master = recv_msg(sock)
        assert resp["status"] == "ok"
        pid = resp["pid"]
        assert os.tcgetpgrp(master) == pid                  # child is the tty's foreground group
        os.close(master)
        os.kill(pid, 9)
    finally:
        sock.close(); proc.terminate(); proc.wait(timeout=5)


def test_spawn_timeout_kills_by_pid_not_group(monkeypatch):
    # On a spawn_timeout the grandchild F is still pre-exec and may not have run setsid(), so
    # its pgid is not guaranteed to equal pid: the broker must os.kill(pid) the single process,
    # never os.killpg(pid) (which could SIGKILL an unrelated process group).
    victim = subprocess.Popen(["sleep", "30"])              # a live pid we fully control
    real_kill = pty_broker.os.kill
    killed = {"kill": [], "killpg": []}
    monkeypatch.setattr(pty_broker.os, "kill",
                        lambda pid, sig: killed["kill"].append((pid, sig)))
    monkeypatch.setattr(pty_broker.os, "killpg",
                        lambda pgid, sig: killed["killpg"].append((pgid, sig)))
    monkeypatch.setattr(pty_broker, "_read_pid", lambda pid_r: victim.pid)
    monkeypatch.setattr(pty_broker, "_read_err",
                        lambda err_r: {"stage": "spawn_timeout", "errno": None})
    try:
        resp, master = pty_broker.handle_spawn(
            {"v": 1, "argv": ["true"], "cwd": None, "env": {}, "cols": 80, "rows": 24})
        assert master is None
        assert resp["status"] == "spawn_failed" and resp["stage"] == "spawn_timeout"
        assert killed["killpg"] == []                       # must NOT target a process group
        assert (victim.pid, signal.SIGKILL) in killed["kill"]
    finally:
        try:
            real_kill(victim.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        victim.wait()
