import re

import pytest
from conftest import EXECUTOR, make_spec
from daemon.events import EventQueue
from daemon.manager import SessionManager


class FakeSession:
    def __init__(self, sid, executor, *a, **k):
        self.sid = sid; self.executor = executor; self.started = None
        self.started_cwd = None; self.stopped = False; self.task = None; self.cwd = None
    def start(self, task, cwd): self.started = task; self.started_cwd = cwd; self.task = task; self.cwd = cwd
    def respond(self, answer, decision_id=None):
        from daemon.session import RespondOutcome
        return RespondOutcome("resumed", seq=1, decision_id="dec-1")
    def snapshot(self): return {"session_id": self.sid, "executor": self.executor,
                                "control_state": "busy", "task_delivery": "pending"}
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
    _out = m.start(EXECUTOR, "task A", "/tmp"); sid = _out.session_id; base_seq = _out.base_seq
    assert captured[0].started == "task A" and m.get(sid) is captured[0]
    assert base_seq == 0                           # daemon-owned cursor: high-water before start
    # session ids are uuid-based (not a per-daemon sequential counter that resets to
    # "s1" on restart and collides with stale references); consistent with evt-<hex>.
    assert re.match(r"^s-[0-9a-f]{8}$", sid), f"non-uuid session id: {sid!r}"
    with pytest.raises(RuntimeError):
        m.start(EXECUTOR, "task B", "/tmp")        # limit reached


def test_unknown_executor_raises():
    m, _ = _mgr()
    with pytest.raises(RuntimeError):
        m.start("nope", "x", "/repo")


def test_base_seq_skips_a_prior_sessions_event():
    # A prior session left an event in the global queue. A new session's base_seq is the
    # high-water at start, so a session-scoped wait from there does NOT surface the stale
    # prior-session event — only this session's future events wake the orchestrator.
    m, _ = _mgr(limit=2)
    prior = m._events.publish("s-old", EXECUTOR, "done", "x", "exited")
    _out = m.start(EXECUTOR, "task", "/tmp"); sid = _out.session_id; base_seq = _out.base_seq
    assert base_seq == prior.seq                              # high-water before the new session
    # nothing for the new session yet -> no wake (the prior event is filtered out by session_id)
    assert m._events.wait_event(after_seq=base_seq, session_id=sid, timeout=0.1) is None
    mine = m._events.publish(sid, EXECUTOR, "waiting_for_user", "?", "idle_prompt")
    assert m._events.wait_event(after_seq=base_seq, session_id=sid, timeout=0.1) is mine


def test_start_threads_cwd_to_session(tmp_path):
    m, captured = _mgr()
    m.start(EXECUTOR, "t", str(tmp_path))
    assert captured[0].started_cwd == str(tmp_path)


def test_start_expands_user_and_makes_cwd_absolute():
    import os
    m, captured = _mgr()
    m.start(EXECUTOR, "t", "~")                 # home exists -> passes validation
    assert captured[0].started_cwd == os.path.expanduser("~")
    assert os.path.isabs(captured[0].started_cwd)


def test_start_rejects_nonexistent_cwd():
    m, captured = _mgr()
    with pytest.raises(ValueError):
        m.start(EXECUTOR, "t", "/no/such/dir/definitely-not-here")
    assert captured == []                       # invalid cwd -> no session created


def test_start_rejects_cwd_that_is_a_file(tmp_path):
    m, captured = _mgr()
    f = tmp_path / "afile"; f.write_text("x")
    with pytest.raises(ValueError):
        m.start(EXECUTOR, "t", str(f))
    assert captured == []


def test_start_failure_does_not_leak_session():
    # If session.start() raises (rejected task / spawn failure), the manager must not leave a
    # registered-but-unstarted session behind, and the concurrency slot must be freed.
    specs = {EXECUTOR: make_spec()}
    q = EventQueue()
    calls = {"n": 0}
    made = []

    class MaybeBoom(FakeSession):
        def start(self, task, cwd):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("rejected")       # first start fails
            super().start(task, cwd)               # later starts behave normally

    def factory(sid, ex, spec, ev):
        s = MaybeBoom(sid, ex); made.append(s); return s

    m = SessionManager(specs, q, session_factory=factory, concurrency_limit=1)
    with pytest.raises(ValueError):
        m.start(EXECUTOR, "task", "/tmp")
    assert m.status()["sessions"] == {}            # no leaked session
    assert made[0].stopped is True                 # partially-started session was torn down
    _out = m.start(EXECUTOR, "task2", "/tmp"); sid = _out.session_id    # slot freed: a fresh start still works
    assert sid is not None and m.status()["sessions"][sid]["control_state"] == "busy"


def test_operator_stop_publishes_single_stopped_event(tmp_path):
    """A live agent stopped by the operator must emit exactly one terminal 'stopped' event (so the
    per-session waiter fires and exits), recorded in recent_terminal, with no deadlock and no double
    done/crashed event. manager.stop() must NOT self-pop — _free_slot captures recent_terminal."""
    events = EventQueue()

    class StoppingSession:
        # Drives the real terminal path: on stop() publish ONE 'stopped' event, then invoke
        # on_terminal (manager._free_slot, which re-enters manager._lock) — exactly what the real
        # monitor's _finish_publish + _finish_cleanup do for an operator stop.
        def __init__(self, sid, executor):
            self.sid = sid; self.executor = executor
            self.on_terminal = None; self.reaper_ctx = None
            self.lineage_id = None; self.restarted_from = None; self.restart_count = 0
        def start(self, task, cwd): pass
        def snapshot(self): return {"session_id": self.sid, "state": "working"}
        def terminal_snapshot(self):
            return {"session_id": self.sid, "state": "stopped", "terminal_kind": "stopped",
                    "terminal": True, "lineage_id": self.lineage_id,
                    "restarted_from": None, "restart_count": 0}
        def stop(self):
            events.publish(self.sid, self.executor, "stopped", "", "stopped")
            if self.on_terminal is not None:
                self.on_terminal(self.sid)

    specs = {EXECUTOR: make_spec()}
    mgr = SessionManager(specs, events, concurrency_limit=2,
                         session_factory=lambda sid, ex, spec, ev: StoppingSession(sid, ex),
                         session_retain=0, session_max_age_days=0)
    _out = mgr.start(EXECUTOR, "t", str(tmp_path)); sid = _out.session_id; base = _out.base_seq
    before = events.latest_seq(sid)

    assert mgr.stop(sid).status in ("stopped", "stop_requested")   # returns, no deadlock

    # exactly one new terminal event for this session, kind == "stopped"
    new = [e for e in events._events if e.session_id == sid and e.seq > before]
    assert [e.kind for e in new] == ["stopped"]
    # a session-scoped waiter parked at `before` would now see it
    assert events.wait_event(after_seq=before, timeout=0, session_id=sid).kind == "stopped"
    # recorded in recent_terminal so the board read can show it
    assert sid in mgr.status()["recent_terminal"]


def test_status_lists_all_and_stop():
    m, captured = _mgr(limit=2)
    _out = m.start(EXECUTOR, "t", "/tmp"); sid = _out.session_id
    all_status = m.status()
    assert sid in all_status["sessions"]
    assert m.stop(sid).status in ("stopped", "stop_requested") and captured[0].stopped is True


import time as _time


def _seed_session(root, name, age_days=0.0, now=None):
    now = _time.time() if now is None else now
    d = root / name; d.mkdir(parents=True)
    tj = d / "transcript.jsonl"; tj.write_text("{}\n")
    ts = now - age_days * 86400
    import os
    os.utime(tj, (ts, ts))
    return d


def test_gc_count_prunes_oldest_keeps_active(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    import paths
    from daemon.manager import gc_sessions
    root = paths.sessions_root()
    now = 1_000_000.0
    for i in range(4):
        _seed_session(root, f"s-{i:08x}", age_days=i, now=now)   # s-0 newest ... s-3 oldest
    active = "s-00000003"  # the OLDEST by mtime, but registered -> must survive
    # 3 inactive dirs (s-0/s-1/s-2); count rake keeps the newest retain=2 of them.
    gc_sessions({active}, retain=2, max_age_days=0, now=now)
    remaining = sorted(p.name for p in root.iterdir())
    assert active in remaining                       # excluded despite being oldest
    assert "s-00000000" in remaining                 # newest kept
    assert "s-00000002" not in remaining             # oldest inactive pruned by count rake
    assert len(remaining) == 3                        # active + newest 2 inactive survivors


def test_gc_age_prunes_old(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    import paths
    from daemon.manager import gc_sessions
    root = paths.sessions_root()
    now = 1_000_000.0
    _seed_session(root, "s-young", age_days=1, now=now)
    _seed_session(root, "s-old", age_days=30, now=now)
    gc_sessions(set(), retain=0, max_age_days=7, now=now)   # retain 0 disables count rake
    names = {p.name for p in root.iterdir()}
    assert names == {"s-young"}


def test_gc_rmtree_failure_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    import paths
    from daemon import manager
    root = paths.sessions_root()
    now = 1_000_000.0
    _seed_session(root, "s-a", age_days=30, now=now)
    monkeypatch.setattr(manager.shutil, "rmtree",
                        lambda d: (_ for _ in ()).throw(OSError("locked")))
    manager.gc_sessions(set(), retain=0, max_age_days=7, now=now)   # must not raise
    assert (root / "s-a").exists()


def test_session_created_and_stopped_logged(tmp_path, monkeypatch):
    import io, json
    from daemon.obs import Logger
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    buf = io.StringIO()
    mgr = SessionManager({EXECUTOR: make_spec()}, EventQueue(), concurrency_limit=1,
                         logger=Logger(level="debug", stream=buf),
                         session_factory=lambda sid, ex, spec, ev: FakeSession(sid, ex))
    _out = mgr.start(EXECUTOR, "hi", str(tmp_path)); sid = _out.session_id      # real dir for the isdir() check
    mgr.stop(sid)
    events = [json.loads(l)["event"] for l in buf.getvalue().splitlines() if l.strip()]
    assert "session_created" in events and "session_stopped" in events


def test_unknown_executor_logs_rejected(tmp_path, monkeypatch):
    import io
    from daemon.obs import Logger
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    buf = io.StringIO()
    mgr = SessionManager({}, EventQueue(), logger=Logger(level="debug", stream=buf))
    try:
        mgr.start("nope", "t", str(tmp_path))
    except Exception:
        pass
    assert "session_start_rejected" in buf.getvalue()


def test_terminal_callback_frees_slot_for_next_start():
    m, captured = _mgr(limit=1)
    _out = m.start(EXECUTOR, "task A", "/tmp"); sid = _out.session_id
    # simulate the session reaching a terminal state and invoking its on_terminal callback
    captured[0].on_terminal(sid)
    assert m.get(sid) is None                            # deregistered
    _out2 = m.start(EXECUTOR, "task B", "/tmp"); sid2 = _out2.session_id        # slot freed -> next start succeeds
    assert sid2 is not None


def test_manager_sets_on_terminal_and_reaper_ctx():
    specs = {EXECUTOR: make_spec()}
    q = EventQueue()
    made = []
    def factory(sid, ex, spec, ev):
        s = FakeSession(sid, ex); made.append(s); return s
    from daemon import reaper
    ctx = reaper.ReaperContext(10, "d1", 5.0, reaper.ProcessInspector(), reaper.ProcessKiller())
    m = SessionManager(specs, q, session_factory=factory, concurrency_limit=1, reaper_ctx=ctx)
    m.start(EXECUTOR, "t", "/tmp")
    assert made[0].reaper_ctx is ctx
    assert callable(made[0].on_terminal)


def test_stop_all_uses_shutdown_reason(tmp_path, monkeypatch):
    import io, json
    from daemon.obs import Logger
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    buf = io.StringIO()
    mgr = SessionManager({EXECUTOR: make_spec()}, EventQueue(), concurrency_limit=1,
                         logger=Logger(level="debug", stream=buf),
                         session_factory=lambda sid, ex, spec, ev: FakeSession(sid, ex))
    mgr.start(EXECUTOR, "hi", str(tmp_path))
    mgr.stop_all()
    rec = [json.loads(l) for l in buf.getvalue().splitlines()
           if json.loads(l)["event"] == "session_stopped"][0]
    assert rec["reason"] == "shutdown"


def test_start_returns_outcome_with_snapshot(tmp_path):
    m, _ = _mgr()
    out = m.start(EXECUTOR, "do it", "/tmp")
    assert out.session_id.startswith("s-")
    assert out.base_seq == 0
    assert out.snapshot["session_id"] == out.session_id
    assert out.snapshot["task_delivery"] == "pending"
    assert out.snapshot["control_state"] == "busy"


def test_stop_unknown_session_outcome(tmp_path):
    m, _ = _mgr()
    out = m.stop("s-nope")
    assert out.status == "unknown_session" and out.snapshot is None


class _StoppedSession:
    def __init__(self, sid, executor, *a, **k):
        self.sid = sid; self.executor = executor; self.on_terminal = None
        self.reaper_ctx = None; self.lineage_id = sid; self.restarted_from = None
        self.restart_count = 0; self.stopped = False
    def start(self, task, cwd): pass
    def snapshot(self): return {"session_id": self.sid, "control_state": "busy",
                                "task_delivery": "delivered"}
    def terminal_snapshot(self):
        return {"session_id": self.sid, "control_state": "terminal", "terminal_kind": "stopped",
                "task_delivery": "delivered", "pending": False, "lineage_id": self.sid,
                "restarted_from": None, "restart_count": 0, "terminal": True}
    def stop(self):
        self.stopped = True
        if self.on_terminal is not None:
            self.on_terminal(self.sid)        # mimic the monitor finalizing -> _free_slot captures


def test_stop_confirmed_terminal_outcome():
    specs = {EXECUTOR: make_spec()}
    def factory(sid, executor, spec, events):
        return _StoppedSession(sid, executor)
    m = SessionManager(specs, EventQueue(), session_factory=factory, concurrency_limit=1)
    out0 = m.start(EXECUTOR, "t", "/tmp")
    out = m.stop(out0.session_id)
    assert out.status == "stopped"
    assert out.snapshot["terminal_kind"] == "stopped"
    assert out.snapshot["control_state"] == "terminal"


def test_restart_outcome_carries_new_snapshot(tmp_path):
    m, _ = _mgr(limit=2)
    out0 = m.start(EXECUTOR, "do it", "/tmp")
    out = m.restart(out0.session_id, force=True)
    assert out.status == "restarted"
    assert out.snapshot["session_id"] == out.session_id
    assert out.snapshot["control_state"] == "busy"
