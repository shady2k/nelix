import re

import pytest
from conftest import EXECUTOR, make_spec
from daemon.events import EventQueue
from daemon.manager import SessionManager


class FakeSession:
    def __init__(self, sid, executor, *a, **k):
        self.sid = sid; self.executor = executor; self.started = None
        self.started_cwd = None; self.stopped = False
    def start(self, task, cwd): self.started = task; self.started_cwd = cwd
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
    sid, base_seq = m.start(EXECUTOR, "task A", "/tmp")
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
    sid, base_seq = m.start(EXECUTOR, "task", "/tmp")
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
    sid, _ = m.start(EXECUTOR, "task2", "/tmp")    # slot freed: a fresh start still works
    assert sid is not None and m.status()["sessions"][sid]["state"] == "working"


def test_status_lists_all_and_stop():
    m, captured = _mgr(limit=2)
    sid, _ = m.start(EXECUTOR, "t", "/tmp")
    all_status = m.status()
    assert sid in all_status["sessions"]
    assert m.stop(sid) is True and captured[0].stopped is True


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
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
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
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
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
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
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
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    buf = io.StringIO()
    mgr = SessionManager({EXECUTOR: make_spec()}, EventQueue(), concurrency_limit=1,
                         logger=Logger(level="debug", stream=buf),
                         session_factory=lambda sid, ex, spec, ev: FakeSession(sid, ex))
    sid, _ = mgr.start(EXECUTOR, "hi", str(tmp_path))      # real dir for the isdir() check
    mgr.stop(sid)
    events = [json.loads(l)["event"] for l in buf.getvalue().splitlines() if l.strip()]
    assert "session_created" in events and "session_stopped" in events


def test_unknown_executor_logs_rejected(tmp_path, monkeypatch):
    import io
    from daemon.obs import Logger
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    buf = io.StringIO()
    mgr = SessionManager({}, EventQueue(), logger=Logger(level="debug", stream=buf))
    try:
        mgr.start("nope", "t", str(tmp_path))
    except Exception:
        pass
    assert "session_start_rejected" in buf.getvalue()


def test_stop_all_uses_shutdown_reason(tmp_path, monkeypatch):
    import io, json
    from daemon.obs import Logger
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    buf = io.StringIO()
    mgr = SessionManager({EXECUTOR: make_spec()}, EventQueue(), concurrency_limit=1,
                         logger=Logger(level="debug", stream=buf),
                         session_factory=lambda sid, ex, spec, ev: FakeSession(sid, ex))
    mgr.start(EXECUTOR, "hi", str(tmp_path))
    mgr.stop_all()
    rec = [json.loads(l) for l in buf.getvalue().splitlines()
           if json.loads(l)["event"] == "session_stopped"][0]
    assert rec["reason"] == "shutdown"
