import os
import time

from daemon.broker_client import set_broker
from daemon.launchers.local import LocalLauncher


class _FakeSpec:
    def argv(self):
        return ["cat"]
    def resolved_env(self):
        return dict(os.environ)


class _FakeBroker:
    def __init__(self):
        self.calls = []
    def spawn(self, argv, cwd, env, cols, rows):
        self.calls.append((argv, cwd, cols, rows))
        master, slave = os.openpty()
        pid = os.fork()
        if pid == 0:
            os.setsid()
            os.dup2(slave, 0); os.dup2(slave, 1); os.dup2(slave, 2)
            os.close(master); os.close(slave)
            os.execvpe("cat", ["cat"], os.environ.copy())
            os._exit(127)
        os.close(slave)
        # The real broker reports pgid == pid (setsid runs before the pid is reported); a plain
        # fork() races the child's setsid(), so wait for it to take effect before capturing pgid.
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                if os.getpgid(pid) == pid:
                    break
            except OSError:
                break
            time.sleep(0.005)
        return master, pid, os.getpgid(pid)


def test_local_launcher_delegates_to_broker(tmp_path):
    fake = _FakeBroker()
    set_broker(fake)
    h = LocalLauncher().start(_FakeSpec(), str(tmp_path), cols=80, rows=24, dialog=None)
    try:
        assert fake.calls == [(["cat"], str(tmp_path), 80, 24)]
        assert h.leader_pid() == h.leader_pgid()
        assert h.is_alive() is True
    finally:
        pid = h.leader_pid()
        h.close()
        os.kill(pid, 9); os.waitpid(pid, 0)
