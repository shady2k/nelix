import re

import pytest
from conftest import EXECUTOR, make_spec
from daemon.events import EventQueue
from daemon.manager import SessionManager


class FakeSession:
    def __init__(self, sid, executor, *a, **k):
        self.sid = sid; self.executor = executor; self.started = None; self.stopped = False
    def start(self, task): self.started = task
    def respond(self, event_id, answer): return event_id == "ok"
    def snapshot(self): return {"session_id": self.sid, "executor": self.executor, "state": "working"}
    def stop(self): self.stopped = True


def _mgr(limit=1):
    specs = {EXECUTOR: make_spec()}
    q = EventQueue()
    captured = []
    def session_factory(sid, executor, spec, events):
        s = FakeSession(sid, executor); captured.append(s); return s
    m = SessionManager(specs, q, session_factory=session_factory, concurrency_limit=limit)
    return m, captured


def test_start_returns_id_and_enforces_limit():
    m, captured = _mgr(limit=1)
    sid = m.start(EXECUTOR, "task A")
    assert captured[0].started == "task A" and m.get(sid) is captured[0]
    # session ids are uuid-based (not a per-daemon sequential counter that resets to
    # "s1" on restart and collides with stale references); consistent with evt-<hex>.
    assert re.match(r"^s-[0-9a-f]{8}$", sid), f"non-uuid session id: {sid!r}"
    with pytest.raises(RuntimeError):
        m.start(EXECUTOR, "task B")        # limit reached


def test_unknown_executor_raises():
    m, _ = _mgr()
    with pytest.raises(RuntimeError):
        m.start("nope", "x")


def test_status_lists_all_and_stop():
    m, captured = _mgr(limit=2)
    sid = m.start(EXECUTOR, "t")
    all_status = m.status()
    assert sid in all_status["sessions"]
    assert m.stop(sid) is True and captured[0].stopped is True
