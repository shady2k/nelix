from daemon.events import EventQueue
from daemon.manager import SessionManager
from daemon.config import ExecutorSpec
from tests.conftest import OWNER, reserve_start


class _FakeSession:
    def __init__(self, sid):
        self.on_terminal = None; self.reaper_ctx = None; self._id = sid
        self.lineage_id = None
    def start(self, task, cwd): pass
    def stop(self): pass
    def observe(self): pass
    def last_observed(self): return 0.0
    def orphan_marked_ts(self): return None
    def mark_orphaned(self, grace): pass
    def snapshot(self): return {"session_id": self._id, "state": "working"}


def _mgr(store_and_ledger, events):
    store, ledger = store_and_ledger
    specs = {"claude": ExecutorSpec(command="c", args=[], env={}, driver="claude")}
    mgr = SessionManager(specs, events, store, concurrency_limit=5,
                          session_factory=lambda sid, ex, spec, ev: _FakeSession(sid),
                          session_retain=0, session_max_age_days=0)
    return mgr, ledger


def test_status_includes_cursor_equal_to_latest_seq(store_and_ledger):
    events = EventQueue()
    mgr, _ledger = _mgr(store_and_ledger, events)
    events.publish("s-x", "claude", "working", "", "working")
    events.publish("s-x", "claude", "working", "", "working")
    out = mgr.status(owner_id=OWNER)
    assert out["cursor"] == events.latest_seq() == 2


def test_status_cursor_zero_when_no_events(store_and_ledger):
    out = _mgr(store_and_ledger, EventQueue())[0].status(owner_id=OWNER)
    assert out["cursor"] == 0


def test_per_session_status_carries_session_cursor(tmp_path, store_and_ledger):
    events = EventQueue()
    mgr, ledger = _mgr(store_and_ledger, events)
    _out = mgr.start("claude", "t", str(tmp_path), owner_id=OWNER,
                     session_id=reserve_start(ledger)); sid = _out.session_id
    events.publish(sid, "claude", "waiting_for_user", "", "working")  # one event for this session
    out = mgr.status(sid, owner_id=OWNER)
    assert out["session_id"] == sid
    assert out["cursor"] == events.latest_seq(sid)        # session-scoped cursor present


def test_per_session_status_unknown_session(store_and_ledger):
    assert _mgr(store_and_ledger, EventQueue())[0].status("s-nope", owner_id=OWNER) == {"error": "unknown session"}


def test_all_sessions_status_carries_per_session_seq(tmp_path, store_and_ledger):
    events = EventQueue()
    mgr, ledger = _mgr(store_and_ledger, events)
    _out = mgr.start("claude", "t", str(tmp_path), owner_id=OWNER,
                     session_id=reserve_start(ledger)); sid = _out.session_id
    events.publish(sid, "claude", "working", "", "working")
    out = mgr.status(owner_id=OWNER)
    assert out["cursor"] == events.latest_seq()           # top-level stays GLOBAL
    assert out["sessions"][sid]["seq"] == events.latest_seq(sid)   # per-session seq added
