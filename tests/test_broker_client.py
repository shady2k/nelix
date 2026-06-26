import os

import pytest

import daemon.broker_client as broker_client
from daemon.broker_client import BrokerClient, BrokerSpawnError, set_broker, get_broker


def test_spawn_returns_live_master_and_pid(tmp_path):
    bc = BrokerClient()
    try:
        master, pid, pgid = bc.spawn(["cat"], str(tmp_path), dict(os.environ), 80, 24)
        assert pid == pgid and os.getpgid(pid) == pgid
        os.write(master, b"yo\n")
        import time; time.sleep(0.3)
        assert b"yo" in os.read(master, 4096)
        os.close(master); os.kill(pid, 9)
    finally:
        bc.close()


def test_spawn_failure_raises(tmp_path):
    bc = BrokerClient()
    try:
        with pytest.raises(BrokerSpawnError):
            bc.spawn(["/nope/nope"], str(tmp_path), dict(os.environ), 80, 24)
    finally:
        bc.close()


def test_lazy_respawn_after_broker_death(tmp_path):
    bc = BrokerClient()
    try:
        bc._proc.kill(); bc._proc.wait(timeout=5)        # simulate broker crash
        master, pid, pgid = bc.spawn(["cat"], str(tmp_path), dict(os.environ), 80, 24)
        assert os.getpgid(pid) == pgid                    # respawned transparently
        os.close(master); os.kill(pid, 9)
    finally:
        bc.close()


def test_spawn_ok_without_fd_raises(tmp_path, monkeypatch):
    # Protocol violation: status "ok" but no master fd. The client must not hand back a broken
    # handle (master=None) — it must raise instead.
    bc = BrokerClient()
    try:
        monkeypatch.setattr(broker_client, "send_msg", lambda *a, **k: None)
        monkeypatch.setattr(broker_client, "recv_msg",
                            lambda *a, **k: ({"status": "ok", "pid": 1, "pgid": 1}, None))
        with pytest.raises(BrokerSpawnError) as ei:
            bc.spawn(["cat"], str(tmp_path), dict(os.environ), 80, 24)
        assert ei.value.stage == "missing_fd"
    finally:
        bc.close()


def test_module_singleton():
    bc = BrokerClient()
    set_broker(bc)
    assert get_broker() is bc
    bc.close()
