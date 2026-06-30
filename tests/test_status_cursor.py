from daemon.events import EventQueue
from daemon.manager import SessionManager
from daemon.config import ExecutorSpec


class _FakeSession:
    def __init__(self, sid):
        self.on_terminal = None; self.reaper_ctx = None; self._id = sid
        self.lineage_id = None
    def start(self, task, cwd): pass
    def stop(self): pass
    def snapshot(self): return {"session_id": self._id, "state": "working"}


def _mgr(events):
    specs = {"claude": ExecutorSpec(command="c", args=[], env={}, driver="claude")}
    return SessionManager(specs, events, concurrency_limit=5,
                          session_factory=lambda sid, ex, spec, ev: _FakeSession(sid),
                          session_retain=0, session_max_age_days=0)


def test_status_includes_cursor_equal_to_latest_seq():
    events = EventQueue()
    mgr = _mgr(events)
    events.publish("s-x", "claude", "working", "", "working")
    events.publish("s-x", "claude", "working", "", "working")
    out = mgr.status()
    assert out["cursor"] == events.latest_seq() == 2


def test_status_cursor_zero_when_no_events():
    out = _mgr(EventQueue()).status()
    assert out["cursor"] == 0


def test_per_session_status_carries_session_cursor(tmp_path):
    events = EventQueue()
    mgr = _mgr(events)
    _out = mgr.start("claude", "t", str(tmp_path)); sid = _out.session_id
    events.publish(sid, "claude", "waiting_for_user", "", "working")  # one event for this session
    out = mgr.status(sid)
    assert out["session_id"] == sid
    assert out["cursor"] == events.latest_seq(sid)        # session-scoped cursor present


def test_per_session_status_unknown_session():
    assert _mgr(EventQueue()).status("s-nope") == {"error": "unknown session"}


def test_all_sessions_status_carries_per_session_seq(tmp_path):
    events = EventQueue()
    mgr = _mgr(events)
    _out = mgr.start("claude", "t", str(tmp_path)); sid = _out.session_id
    events.publish(sid, "claude", "working", "", "working")
    out = mgr.status()
    assert out["cursor"] == events.latest_seq()           # top-level stays GLOBAL
    assert out["sessions"][sid]["seq"] == events.latest_seq(sid)   # per-session seq added
