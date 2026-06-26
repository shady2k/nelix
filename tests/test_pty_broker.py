import os
import subprocess
import sys
import time

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
