import os
import time

from daemon.pty_session import PtySession


def _spawn_cat():
    # Build a master fd + a real child the way the broker will (minus the double-fork;
    # the test is single-threaded so a plain fork is safe here).
    master, slave = os.openpty()
    pid = os.fork()
    if pid == 0:
        os.setsid()
        os.dup2(slave, 0); os.dup2(slave, 1); os.dup2(slave, 2)
        os.close(master); os.close(slave)
        os.execvpe("cat", ["cat"], os.environ.copy())
        os._exit(127)
    os.close(slave)
    # The broker reports pgid == pid (setsid runs in the child BEFORE the pid is reported).
    # A plain fork() races the child's setsid(), so wait for it to take effect before the
    # parent captures os.getpgid(pid) -- otherwise it may read the pre-setsid inherited pgid.
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            if os.getpgid(pid) == pid:
                break
        except OSError:
            break
        time.sleep(0.005)
    return master, pid


def test_pump_render_write_roundtrip():
    master, pid = _spawn_cat()
    s = PtySession(master, pid, os.getpgid(pid))
    try:
        s.write("hello\n")
        deadline = time.time() + 5
        while time.time() < deadline and "hello" not in s.render():
            s.pump(0.1)
        assert "hello" in s.render()
        assert s.is_alive() is True
        st = s.leader_status()
        assert st.alive is True and st.status_available is False
        assert s.leader_pid() == pid and s.leader_pgid() == os.getpgid(pid)
    finally:
        s.close()
        try:
            os.kill(pid, 9); os.waitpid(pid, 0)
        except OSError:
            pass


def test_eof_marks_dead():
    master, pid = _spawn_cat()
    s = PtySession(master, pid, os.getpgid(pid))
    os.kill(pid, 9)
    os.waitpid(pid, 0)                       # reap so the slave hangs up
    deadline = time.time() + 5
    while time.time() < deadline and s.is_alive():
        s.pump(0.1)                          # pump observes EOF on the master
    assert s.is_alive() is False
    assert s.leader_status().alive is False
    s.close()
