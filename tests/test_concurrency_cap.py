import textwrap
import pytest
from daemon.config import load_concurrency_limit


def _toml(tmp_path, body):
    p = tmp_path / "nelix.toml"
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_concurrency_limit_defaults_to_5_when_unset(tmp_path):
    path = _toml(tmp_path, """
        [executors.claude]
        command = "claude"
        driver = "claude"
    """)
    assert load_concurrency_limit(path) == 5


def test_concurrency_limit_missing_file_defaults_to_5(tmp_path):
    assert load_concurrency_limit(str(tmp_path / "nope.toml")) == 5


def test_concurrency_limit_explicit_value_honoured(tmp_path):
    path = _toml(tmp_path, "concurrency_limit = 3\n")
    assert load_concurrency_limit(path) == 3


def test_concurrency_limit_invalid_falls_back_to_5(tmp_path):
    path = _toml(tmp_path, 'concurrency_limit = "lots"\n')
    assert load_concurrency_limit(path) == 5


from daemon.events import EventQueue
from tests.conftest import OWNER, reserve_start


class _FakeSession:
    def __init__(self, sid, executor, spec):
        self.on_terminal = None
        self.reaper_ctx = None
        self._id = sid
        self.started = False
    def start(self, task, cwd):
        self.started = True
    def stop(self): pass
    def snapshot(self): return {"session_id": self._id, "control_state": "busy", "task_delivery": "pending"}


def _manager(tmp_path, store_and_ledger, limit):
    from daemon.manager import SessionManager
    from daemon.config import ExecutorSpec
    store, ledger = store_and_ledger
    specs = {"claude": ExecutorSpec(command="claude", args=[], env={}, driver="claude")}
    events = EventQueue()
    mgr = SessionManager(specs, events, store, concurrency_limit=limit,
                          session_factory=lambda sid, ex, spec, ev: _FakeSession(sid, ex, spec),
                          session_retain=0, session_max_age_days=0)
    return mgr, ledger


def test_cap_admits_N_rejects_N_plus_1(tmp_path, store_and_ledger):
    mgr, ledger = _manager(tmp_path, store_and_ledger, limit=2)
    cwd = str(tmp_path)
    mgr.start("claude", "t1", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    mgr.start("claude", "t2", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    with pytest.raises(RuntimeError, match="concurrency_limit=2 reached"):
        mgr.start("claude", "t3", cwd, owner_id=OWNER, session_id=reserve_start(ledger))


