import json
import paths
from daemon.config import ExecutorSpec
from daemon.events import EventQueue
from daemon.session import Session
from daemon.drivers.base import Driver


class _NoopLauncher:
    def start(self, spec, cwd, cols, rows, dialog=None):
        return _NoopHandle()
    def stop(self, handle):
        pass


class _NoopHandle:
    def is_alive(self): return True
    def render(self): return ""
    def leader_pid(self): return None
    def leader_pgid(self): return None
    def pump(self, t): return False


def _spec():
    return ExecutorSpec(command="claude", args=[], env={}, driver="claude")


def _session(tmp_path, monkeypatch, sid="s-aaaa0001"):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spec = _spec()
    # Minimal driver double: only command_prefixes is needed before the monitor runs.
    class _Drv:
        command_prefixes = ()
        submit_key = "\r"
        def __init__(self): self._settle = 0
    sess = Session(sid, "claude", _Drv(), _NoopLauncher(), spec, EventQueue())
    return sess


def test_snapshot_includes_task_and_cwd(tmp_path, monkeypatch):
    sess = _session(tmp_path, monkeypatch)
    sess.start("fix the login bug", str(tmp_path))
    sess.stop()
    snap = sess.snapshot()
    assert snap["task"] == "fix the login bug"
    assert snap["cwd"] == str(tmp_path)


def test_meta_file_persists_task_cwd_lineage(tmp_path, monkeypatch):
    sess = _session(tmp_path, monkeypatch)
    sess.lineage_id = "s-aaaa0001"
    sess.restarted_from = None
    sess.start("do the thing", str(tmp_path))
    sess.stop()
    meta = json.loads(paths.session_meta(paths.sessions_root() / "s-aaaa0001").read_text())
    assert meta["task"] == "do the thing"
    assert meta["cwd"] == str(tmp_path)
    assert meta["lineage_id"] == "s-aaaa0001"
    assert meta["restarted_from"] is None
    assert meta["executor"] == "claude"


def test_properties_expose_executor_task_cwd(tmp_path, monkeypatch):
    sess = _session(tmp_path, monkeypatch)
    sess.start("a task", str(tmp_path))
    sess.stop()
    assert sess.executor == "claude"
    assert sess.task == "a task"
    assert sess.cwd == str(tmp_path)
