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
    sid = m.start(EXECUTOR, "task A", "/tmp")
    assert captured[0].started == "task A" and m.get(sid) is captured[0]
    # session ids are uuid-based (not a per-daemon sequential counter that resets to
    # "s1" on restart and collides with stale references); consistent with evt-<hex>.
    assert re.match(r"^s-[0-9a-f]{8}$", sid), f"non-uuid session id: {sid!r}"
    with pytest.raises(RuntimeError):
        m.start(EXECUTOR, "task B", "/tmp")        # limit reached


def test_unknown_executor_raises():
    m, _ = _mgr()
    with pytest.raises(RuntimeError):
        m.start("nope", "x", "/repo")


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


def test_status_lists_all_and_stop():
    m, captured = _mgr(limit=2)
    sid = m.start(EXECUTOR, "t", "/tmp")
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
