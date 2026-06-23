import pytest
from daemon.launchers import get_launcher
from daemon.launchers.base import ExecutorCapabilities
from daemon.config import ExecutorSpec


def test_local_launcher_capabilities():
    lr = get_launcher("local")
    assert isinstance(lr.capabilities, ExecutorCapabilities)
    assert lr.capabilities.isolation_class == "host"
    assert lr.capabilities.can_attach is False


def test_unknown_launcher_raises():
    with pytest.raises(ValueError):
        get_launcher("warpdrive")


def test_local_launcher_start_spawns(monkeypatch):
    spawned = {}

    class FakePty:
        def __init__(self, argv, cwd=None, cols=120, rows=40, env=None):
            spawned["argv"] = argv; spawned["cwd"] = cwd; spawned["env"] = env
        def spawn(self): spawned["spawned"] = True
        def close(self): spawned["closed"] = True

    monkeypatch.setattr("daemon.launchers.local.PtySession", FakePty)
    spec = ExecutorSpec(command="tool", args=["-x"], env={}, cwd="/tmp", driver="claude",
                        launcher="local")
    lr = get_launcher("local")
    h = lr.start(spec, cols=100, rows=30)
    assert spawned["spawned"] is True and spawned["argv"] == ["tool", "-x"]
    lr.stop(h)
    assert spawned["closed"] is True
