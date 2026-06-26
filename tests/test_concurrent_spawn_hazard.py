import os
import threading
import time

from daemon.broker_client import BrokerClient
from daemon.pty_session import PtySession


def test_concurrent_spawns_under_pumping_never_hang(tmp_path):
    """Reproduces the fork-under-threads hazard: many monitor threads pumping (heavy pyte
    allocation) while new spawns happen concurrently. With the broker, every spawn must
    succeed and no spawn may hang. Pre-broker this would intermittently deadlock/crash."""
    bc = BrokerClient()
    sessions, errors = [], []
    stop = threading.Event()

    def pumper(s):
        while not stop.is_set():
            try:
                os.write(s._fd, b"x" * 256)        # keep the child echoing -> pyte churns
            except OSError:
                pass
            s.pump(0.02)

    def spawn_one(i):
        try:
            master, pid, pgid = bc.spawn(["cat"], str(tmp_path), dict(os.environ), 80, 24)
            s = PtySession(master, pid, pgid)
            sessions.append(s)
            threading.Thread(target=pumper, args=(s,), daemon=True).start()
        except Exception as e:                      # noqa: BLE001 — capture any hang-surrogate error
            errors.append(repr(e))

    try:
        # Warm up a few pumping sessions, THEN hammer concurrent spawns while they pump.
        for i in range(3):
            spawn_one(i)
        time.sleep(0.2)
        threads = [threading.Thread(target=spawn_one, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
            assert not t.is_alive(), "a spawn hung — fork-under-threads regression"
        assert errors == []
        assert len(sessions) == 13
        for s in sessions:
            assert s.leader_pid() == s.leader_pgid()
    finally:
        stop.set()
        for s in sessions:
            pid = s.leader_pid(); s.close()
            try:
                os.kill(pid, 9); os.waitpid(pid, 0)
            except OSError:
                pass
        bc.close()
