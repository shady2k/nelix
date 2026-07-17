import json
import paths
from daemon.config import ExecutorSpec
from daemon.events import EventQueue
from daemon.session import Session


class _NoopLauncher:
    def start(self, spec, cwd, cols, rows, dialog=None, transcript=None, **_kw):
        return _NoopHandle()
    def stop(self, handle):
        pass


class _NoopHandle:
    def is_alive(self): return True
    def render(self): return ""
    def leader_pid(self): return None
    def leader_pgid(self): return None
    def pump(self, t): return False
    def finalize(self): pass
    def leader_status(self):
        # _finish() calls this during finalization; without it the monitor thread raised
        # AttributeError (a noisy PytestUnhandledThreadExceptionWarning). No-op clean shape.
        from daemon.launchers.base import LeaderStatus
        return LeaderStatus(alive=self.is_alive(), exit_code=None, signal=None,
                            status_available=False)


def _spec():
    return ExecutorSpec(command="claude", args=[], env={}, driver="claude")


def _session(tmp_path, monkeypatch, sid="s-aaaa0001"):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
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


def test_meta_persists_model_override(tmp_path, monkeypatch):
    # nelix-9k0 FIX 1: the manager-set per-session model override is persisted so a from-crash
    # restart (which reads meta off disk) re-injects the SAME model instead of the executor default.
    sess = _session(tmp_path, monkeypatch)
    sess.lineage_id = "s-aaaa0001"
    sess.model = "haiku"
    sess.start("do the thing", str(tmp_path))
    sess.stop()
    meta = json.loads(paths.session_meta(paths.sessions_root() / "s-aaaa0001").read_text())
    assert meta["model"] == "haiku"


def test_meta_model_defaults_to_none_when_unset(tmp_path, monkeypatch):
    sess = _session(tmp_path, monkeypatch)
    sess.lineage_id = "s-aaaa0001"
    sess.start("do the thing", str(tmp_path))         # no model set -> None (no override)
    sess.stop()
    meta = json.loads(paths.session_meta(paths.sessions_root() / "s-aaaa0001").read_text())
    assert meta["model"] is None


def test_properties_expose_executor_task_cwd(tmp_path, monkeypatch):
    sess = _session(tmp_path, monkeypatch)
    sess.start("a task", str(tmp_path))
    sess.stop()
    assert sess.executor == "claude"
    assert sess.task == "a task"
    assert sess.cwd == str(tmp_path)


def test_meta_persists_no_env_or_env_cmd_secret(tmp_path, monkeypatch):
    # nelix-c5o §5: _write_meta must NEVER serialize env — neither the static [env] nor the env_cmd
    # command (which can carry a secret path). On restart the command is RE-RUN, never read from disk.
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    spec = ExecutorSpec(command="claude", args=[], env={"STATIC_SECRET": "s3cr3t-static-value"},
                        driver="claude", env_cmd={"TOK": "print-the-secret-token"})

    class _Drv:
        command_prefixes = ()
        submit_key = "\r"
        def __init__(self): self._settle = 0

    sess = Session("s-envc0001", "claude", _Drv(), _NoopLauncher(), spec, EventQueue())
    sess.lineage_id = "s-envc0001"
    sess.start("do the thing", str(tmp_path))
    sess.stop()
    raw = paths.session_meta(paths.sessions_root() / "s-envc0001").read_text()
    meta = json.loads(raw)
    assert "env" not in meta and "env_cmd" not in meta
    assert "s3cr3t-static-value" not in raw          # the static secret value never persisted
    assert "print-the-secret-token" not in raw       # nor the env_cmd command string
