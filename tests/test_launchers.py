import pytest
from conftest import make_spec
from daemon.launchers import get_launcher
from daemon.launchers.base import ExecutorCapabilities


def test_local_launcher_capabilities():
    lr = get_launcher("local")
    assert isinstance(lr.capabilities, ExecutorCapabilities)
    assert lr.capabilities.isolation_class == "host"
    assert lr.capabilities.can_attach is False


def test_unknown_launcher_raises():
    with pytest.raises(ValueError):
        get_launcher("warpdrive")


def test_auto_launcher_resolves_to_local(monkeypatch):
    # the ExecutorSpec default (launcher omitted -> "auto") must spawn, not raise "unknown launcher"
    monkeypatch.setenv("TERMINAL_ENV", "local")
    lr = get_launcher("auto")
    assert isinstance(lr.capabilities, ExecutorCapabilities)
    assert lr.capabilities.isolation_class == "host"


def test_auto_launcher_under_docker_fails_closed(monkeypatch):
    # a non-local backend must still fail closed at the daemon launcher (docker is post-MVP)
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    with pytest.raises(NotImplementedError):
        get_launcher("auto")


def test_local_launcher_start_spawns(monkeypatch):
    # The launcher no longer forks: it delegates the spawn to the broker and wraps the
    # returned (master_fd, pid, pgid) in a PtySession.
    import daemon.broker_client as bc
    spawned = {}

    class FakeBroker:
        def spawn(self, argv, cwd, env, cols, rows):
            spawned["argv"] = argv; spawned["cwd"] = cwd; spawned["env"] = env
            spawned["cols"] = cols; spawned["rows"] = rows
            return 7, 111, 111                       # master_fd, pid, pgid

    class FakePty:
        def __init__(self, master_fd, pid, pgid, cols=120, rows=40, dialog=None):
            spawned["master_fd"] = master_fd; spawned["pid"] = pid; spawned["pgid"] = pgid
            spawned["dialog"] = dialog
        def close(self): spawned["closed"] = True

    monkeypatch.setattr(bc, "_broker", FakeBroker())     # auto-restored by monkeypatch
    monkeypatch.setattr("daemon.launchers.local.PtySession", FakePty)
    spec = make_spec(command="tool", args=["-x"])
    lr = get_launcher("local")
    h = lr.start(spec, "/tmp", cols=100, rows=30)
    assert spawned["argv"] == ["tool", "-x"] and spawned["cwd"] == "/tmp"
    assert spawned["master_fd"] == 7 and spawned["pid"] == 111 and spawned["pgid"] == 111
    lr.stop(h)
    assert spawned["closed"] is True


def test_local_launcher_uses_given_cwd(monkeypatch):
    import daemon.broker_client as bc
    seen = {}

    class FakeBroker:
        def spawn(self, argv, cwd, env, cols, rows):
            seen["cwd"] = cwd
            return 7, 111, 111

    class FakePty:
        def __init__(self, master_fd, pid, pgid, cols=120, rows=40, dialog=None):
            pass
        def close(self): pass

    monkeypatch.setattr(bc, "_broker", FakeBroker())
    monkeypatch.setattr("daemon.launchers.local.PtySession", FakePty)
    get_launcher("local").start(make_spec(command="tool"), "/work/repo")
    assert seen["cwd"] == "/work/repo"
