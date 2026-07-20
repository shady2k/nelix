import re

import pytest
from tests.conftest import EXECUTOR, OWNER, make_spec, reserve_start
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
    def observe(self): pass
    def last_observed(self): return 0.0
    def orphan_marked_ts(self): return None
    def mark_orphaned(self, grace): pass


def _mgr(store_and_ledger, limit=1):
    store, ledger = store_and_ledger
    specs = {EXECUTOR: make_spec()}
    q = EventQueue()
    captured = []
    def session_factory(sid, executor, spec, events):
        s = FakeSession(sid, executor); captured.append(s); return s
    m = SessionManager(specs, q, store, session_factory=session_factory, concurrency_limit=limit)
    return m, captured, ledger


def test_start_returns_id_and_enforces_limit(store_and_ledger):
    m, captured, ledger = _mgr(store_and_ledger, limit=1)
    sid = reserve_start(ledger)
    _out = m.start(EXECUTOR, "task A", "/tmp", owner_id=OWNER, session_id=sid)
    assert _out.session_id == sid
    assert captured[0].started == "task A" and m.get(sid) is captured[0]
    assert _out.base_seq == 0
    assert re.match(r"^s-[0-9a-f]{32}$", sid), f"non-uuid session id: {sid!r}"
    sid2 = reserve_start(ledger)
    with pytest.raises(RuntimeError):
        m.start(EXECUTOR, "task B", "/tmp", owner_id=OWNER, session_id=sid2)  # limit reached


def test_unknown_executor_raises(store_and_ledger):
    m, _, ledger = _mgr(store_and_ledger)
    sid = reserve_start(ledger)
    with pytest.raises(RuntimeError):
        m.start("nope", "x", "/repo", owner_id=OWNER, session_id=sid)


def test_base_seq_skips_a_prior_sessions_event(store_and_ledger):
    m, _, ledger = _mgr(store_and_ledger, limit=2)
    prior = m._events.publish("s-old", EXECUTOR, "done", "x", "exited")
    sid = reserve_start(ledger)
    _out = m.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
    assert _out.base_seq == prior.seq
    assert m._events.wait_event(after_seq=_out.base_seq, session_id=sid, timeout=0.1) is None
    mine = m._events.publish(sid, EXECUTOR, "waiting_for_user", "?", "idle_prompt")
    assert m._events.wait_event(after_seq=_out.base_seq, session_id=sid, timeout=0.1) is mine


def test_start_threads_cwd_to_session(tmp_path, store_and_ledger):
    m, captured, ledger = _mgr(store_and_ledger)
    sid = reserve_start(ledger)
    m.start(EXECUTOR, "t", str(tmp_path), owner_id=OWNER, session_id=sid)
    assert captured[0].started_cwd == str(tmp_path)


def test_start_expands_user_and_makes_cwd_absolute(store_and_ledger):
    import os
    m, captured, ledger = _mgr(store_and_ledger)
    sid = reserve_start(ledger)
    m.start(EXECUTOR, "t", "~", owner_id=OWNER, session_id=sid)
    assert captured[0].started_cwd == os.path.expanduser("~")
    assert os.path.isabs(captured[0].started_cwd)


def test_start_rejects_nonexistent_cwd(store_and_ledger):
    m, captured, ledger = _mgr(store_and_ledger)
    sid = reserve_start(ledger)
    with pytest.raises(ValueError):
        m.start(EXECUTOR, "t", "/no/such/dir/definitely-not-here", owner_id=OWNER, session_id=sid)
    assert captured == []


def test_start_rejects_cwd_that_is_a_file(tmp_path, store_and_ledger):
    m, captured, ledger = _mgr(store_and_ledger)
    f = tmp_path / "afile"; f.write_text("x")
    sid = reserve_start(ledger)
    with pytest.raises(ValueError):
        m.start(EXECUTOR, "t", str(f), owner_id=OWNER, session_id=sid)
    assert captured == []


def test_start_failure_does_not_leak_session(store_and_ledger):
    store, ledger = store_and_ledger
    specs = {EXECUTOR: make_spec()}
    q = EventQueue()
    calls = {"n": 0}
    made = []

    class MaybeBoom(FakeSession):
        def start(self, task, cwd):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("rejected")
            super().start(task, cwd)

    def factory(sid, ex, spec, ev):
        s = MaybeBoom(sid, ex); made.append(s); return s

    m = SessionManager(specs, q, store, session_factory=factory, concurrency_limit=1)
    sid = reserve_start(ledger)
    with pytest.raises(ValueError):
        m.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
    assert m.status(owner_id=OWNER)["sessions"] == {}
    assert made[0].stopped is True
    sid2 = reserve_start(ledger)
    _out = m.start(EXECUTOR, "task2", "/tmp", owner_id=OWNER, session_id=sid2)
    assert _out.session_id is not None
    assert m.status(owner_id=OWNER)["sessions"][sid2]["control_state"] == "busy"


def test_operator_stop_publishes_single_stopped_event(tmp_path, store_and_ledger):
    store, ledger = store_and_ledger
    events = EventQueue()

    class StoppingSession:
        def __init__(self, sid, executor):
            self.sid = sid; self.executor = executor
            self.on_terminal = None; self.reaper_ctx = None
            self.lineage_id = None; self.restarted_from = None; self.restart_count = 0
        def start(self, task, cwd): pass
        def snapshot(self): return {"session_id": self.sid, "state": "working"}
        def terminal_snapshot(self):
            return {"session_id": self.sid, "state": "stopped", "terminal_kind": "stopped",
                    "terminal": True, "lineage_id": self.lineage_id,
                    "restarted_from": None, "restart_count": 0,
                    "screen_excerpt": ""}
        def stop(self):
            events.publish(self.sid, self.executor, "stopped", "", "stopped")
            if self.on_terminal is not None:
                self.on_terminal(self.sid)
        def observe(self): pass
        def last_observed(self): return 0.0
        def orphan_marked_ts(self): return None
        def mark_orphaned(self, grace): pass

    specs = {EXECUTOR: make_spec()}
    mgr = SessionManager(specs, events, store, concurrency_limit=2,
                         session_factory=lambda sid, ex, spec, ev: StoppingSession(sid, ex),
                         session_retain=0, session_max_age_days=0)
    sid = reserve_start(ledger)
    _out = mgr.start(EXECUTOR, "t", str(tmp_path), owner_id=OWNER, session_id=sid)
    before = events.latest_seq(sid)

    assert mgr.stop(sid, owner_id=OWNER).status in ("stopped", "stop_requested")

    new = [e for e in events._events if e.session_id == sid and e.seq > before]
    assert [e.kind for e in new] == ["stopped"]
    assert events.wait_event(after_seq=before, timeout=0, session_id=sid).kind == "stopped"
    # S2a.2: daemon no longer surfaces persisted terminals in recent_terminal.
    # Verify the store has the terminal record instead.
    term = store.get_terminal(sid, owner_id=OWNER)
    assert term.terminal_kind == "stopped"


def test_status_lists_all_and_stop(store_and_ledger):
    m, captured, ledger = _mgr(store_and_ledger, limit=2)
    sid = reserve_start(ledger)
    _out = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id=sid)
    all_status = m.status(owner_id=OWNER)
    assert sid in all_status["sessions"]
    assert m.stop(sid, owner_id=OWNER).status in ("stopped", "stop_requested") and captured[0].stopped is True


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
        _seed_session(root, f"s-{i:08x}", age_days=i, now=now)
    active = "s-00000003"
    gc_sessions({active}, retain=2, max_age_days=0, now=now)
    remaining = sorted(p.name for p in root.iterdir())
    assert active in remaining
    assert "s-00000000" in remaining
    assert "s-00000002" not in remaining
    assert len(remaining) == 3


def test_gc_age_prunes_old(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    import paths
    from daemon.manager import gc_sessions
    root = paths.sessions_root()
    now = 1_000_000.0
    _seed_session(root, "s-young", age_days=1, now=now)
    _seed_session(root, "s-old", age_days=30, now=now)
    gc_sessions(set(), retain=0, max_age_days=7, now=now)
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
    manager.gc_sessions(set(), retain=0, max_age_days=7, now=now)
    assert (root / "s-a").exists()


def test_session_created_and_stopped_logged(tmp_path, store_and_ledger, monkeypatch):
    import io, json
    from daemon.obs import Logger
    store, ledger = store_and_ledger
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    buf = io.StringIO()
    mgr = SessionManager({EXECUTOR: make_spec()}, EventQueue(), store, concurrency_limit=1,
                         logger=Logger(level="debug", stream=buf),
                         session_factory=lambda sid, ex, spec, ev: FakeSession(sid, ex))
    sid = reserve_start(ledger)
    _out = mgr.start(EXECUTOR, "hi", str(tmp_path), owner_id=OWNER, session_id=sid)
    mgr.stop(sid, owner_id=OWNER)
    events = [json.loads(l)["event"] for l in buf.getvalue().splitlines() if l.strip()]
    assert "session_created" in events and "session_stopped" in events


def test_unknown_executor_logs_rejected(tmp_path, store_and_ledger, monkeypatch):
    import io
    from daemon.obs import Logger
    store, ledger = store_and_ledger
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    buf = io.StringIO()
    mgr = SessionManager({}, EventQueue(), store, logger=Logger(level="debug", stream=buf))
    sid = reserve_start(ledger)
    try:
        mgr.start("nope", "t", str(tmp_path), owner_id=OWNER, session_id=sid)
    except Exception:
        pass
    assert "session_start_rejected" in buf.getvalue()


def test_terminal_callback_frees_slot_for_next_start(store_and_ledger):
    m, captured, ledger = _mgr(store_and_ledger, limit=1)
    sid = reserve_start(ledger)
    _out = m.start(EXECUTOR, "task A", "/tmp", owner_id=OWNER, session_id=sid)
    captured[0].on_terminal(sid)
    assert m.get(sid) is None
    sid2 = reserve_start(ledger)
    _out2 = m.start(EXECUTOR, "task B", "/tmp", owner_id=OWNER, session_id=sid2)
    assert sid2 is not None


def test_manager_sets_on_terminal_and_reaper_ctx(store_and_ledger):
    store, ledger = store_and_ledger
    specs = {EXECUTOR: make_spec()}
    q = EventQueue()
    made = []
    def factory(sid, ex, spec, ev):
        s = FakeSession(sid, ex); made.append(s); return s
    from daemon import reaper
    ctx = reaper.ReaperContext(10, "d1", 5.0, reaper.ProcessInspector(), reaper.ProcessKiller())
    m = SessionManager(specs, q, store, session_factory=factory, concurrency_limit=1, reaper_ctx=ctx)
    sid = reserve_start(ledger)
    m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id=sid)
    assert made[0].reaper_ctx is ctx
    assert callable(made[0].on_terminal)


def test_stop_all_uses_shutdown_reason(tmp_path, store_and_ledger, monkeypatch):
    import io, json
    from daemon.obs import Logger
    store, ledger = store_and_ledger
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    buf = io.StringIO()
    mgr = SessionManager({EXECUTOR: make_spec()}, EventQueue(), store, concurrency_limit=1,
                         logger=Logger(level="debug", stream=buf),
                         session_factory=lambda sid, ex, spec, ev: FakeSession(sid, ex))
    sid = reserve_start(ledger)
    mgr.start(EXECUTOR, "hi", str(tmp_path), owner_id=OWNER, session_id=sid)
    mgr.stop_all()
    rec = [json.loads(l) for l in buf.getvalue().splitlines()
           if json.loads(l)["event"] == "session_stopped"][0]
    assert rec["reason"] == "shutdown"


def test_start_returns_outcome_with_snapshot(store_and_ledger):
    m, _, ledger = _mgr(store_and_ledger)
    sid = reserve_start(ledger)
    out = m.start(EXECUTOR, "do it", "/tmp", owner_id=OWNER, session_id=sid)
    assert out.session_id == sid
    assert out.base_seq == 0
    assert out.snapshot["session_id"] == sid
    assert out.snapshot["task_delivery"] == "pending"
    assert out.snapshot["control_state"] == "busy"


def test_stop_unknown_session_outcome(store_and_ledger):
    m, _, _ = _mgr(store_and_ledger)
    out = m.stop("s-nope", owner_id=OWNER)
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
                "restarted_from": None, "restart_count": 0, "terminal": True,
                "screen_excerpt": ""}
    def stop(self):
        self.stopped = True
        if self.on_terminal is not None:
            self.on_terminal(self.sid)
    def observe(self): pass
    def last_observed(self): return 0.0
    def orphan_marked_ts(self): return None
    def mark_orphaned(self, grace): pass


def test_stop_confirmed_terminal_outcome(store_and_ledger):
    store, ledger = store_and_ledger
    specs = {EXECUTOR: make_spec()}
    def factory(sid, executor, spec, events):
        return _StoppedSession(sid, executor)
    m = SessionManager(specs, EventQueue(), store, session_factory=factory, concurrency_limit=1)
    sid = reserve_start(ledger)
    out0 = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id=sid)
    out = m.stop(out0.session_id, owner_id=OWNER)
    assert out.status == "stopped"
    assert out.snapshot["terminal_kind"] == "stopped"
    assert out.snapshot["control_state"] == "terminal"


def test_restart_outcome_carries_new_snapshot(store_and_ledger):
    m, _, ledger = _mgr(store_and_ledger, limit=2)
    sid = reserve_start(ledger)
    out0 = m.start(EXECUTOR, "do it", "/tmp", owner_id=OWNER, session_id=sid)
    new_sid = reserve_start(ledger)
    out = m.restart(out0.session_id, new_session_id=new_sid, force=True, owner_id=OWNER)
    assert out.status == "restarted"
    assert out.snapshot["session_id"] == out.session_id
    assert out.snapshot["control_state"] == "busy"
