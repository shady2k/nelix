from daemon.events import EventQueue
from daemon.manager import SessionManager
from daemon.config import ExecutorSpec


class _FakeSession:
    def __init__(self, sid, state="crashed", terminal_kind="crashed"):
        self.on_terminal = None; self.reaper_ctx = None; self._id = sid
        self.lineage_id = sid; self.restarted_from = None; self.restart_count = 0
        self._state = state; self._terminal_kind = terminal_kind
    def start(self, task, cwd): pass
    def stop(self): pass
    def snapshot(self): return {"session_id": self._id, "state": self._state}
    def terminal_snapshot(self):
        return {"session_id": self._id, "executor": "claude", "task": "t", "cwd": "/p",
                "state": self._state, "terminal_kind": self._terminal_kind,
                "screen_excerpt": "boom", "lineage_id": self.lineage_id,
                "restarted_from": None, "restart_count": 0, "terminal": True}


def _mgr(ttl=300.0, clock=None):
    specs = {"claude": ExecutorSpec(command="c", args=[], env={}, driver="claude")}
    return SessionManager(specs, EventQueue(), concurrency_limit=5,
                          session_factory=lambda sid, ex, spec, ev: _FakeSession(sid),
                          session_retain=0, session_max_age_days=0,
                          terminal_snapshot_ttl=ttl, clock=(clock or (lambda: 1000.0)))


def test_free_slot_captures_terminal_snapshot_and_frees_slot():
    mgr = _mgr()
    sess = _FakeSession("s-1"); sess.on_terminal = mgr._free_slot
    mgr._sessions["s-1"] = sess
    mgr._free_slot("s-1")
    assert "s-1" not in mgr._sessions                       # slot freed
    out = mgr.status()
    assert out["recent_terminal"]["s-1"]["state"] == "crashed"
    assert out["recent_terminal"]["s-1"]["screen_excerpt"] == "boom"


def test_terminal_snapshot_pruned_after_ttl():
    t = {"now": 1000.0}
    mgr = _mgr(ttl=10.0, clock=lambda: t["now"])
    sess = _FakeSession("s-1"); mgr._sessions["s-1"] = sess
    mgr._free_slot("s-1")
    assert "s-1" in mgr.status()["recent_terminal"]
    t["now"] = 1011.0                                       # past ttl
    assert "s-1" not in mgr.status().get("recent_terminal", {})


def test_multiple_terminal_snapshots_coexist_and_prune_independently():
    t = {"now": 1000.0}
    mgr = _mgr(ttl=10.0, clock=lambda: t["now"])
    for sid in ("s-1", "s-2"):
        mgr._sessions[sid] = _FakeSession(sid)
    mgr._free_slot("s-1")
    t["now"] = 1005.0
    mgr._free_slot("s-2")
    t["now"] = 1011.0                                       # s-1 expired (>10s), s-2 not (6s)
    rt = mgr.status()["recent_terminal"]
    assert "s-1" not in rt and "s-2" in rt


def test_negative_ttl_does_not_store_terminal_snapshot():
    mgr = _mgr(ttl=-5)
    sess = _FakeSession("s-1"); mgr._sessions["s-1"] = sess
    mgr._free_slot("s-1")
    assert "s-1" not in mgr._sessions                       # slot freed
    assert "s-1" not in mgr._terminal                       # snapshot not stored at all


# --- terminal_kind regression tests ---

def test_terminal_kind_delivery_failed_plumbed_through_status():
    mgr = _mgr()
    sess = _FakeSession("s-df", state="working", terminal_kind="delivery_failed")
    mgr._sessions["s-df"] = sess
    mgr._free_slot("s-df")
    rt = mgr.status()["recent_terminal"]
    assert rt["s-df"]["terminal_kind"] == "delivery_failed"


def test_terminal_kind_done_plumbed_through_status():
    mgr = _mgr()
    sess = _FakeSession("s-ok", state="exited", terminal_kind="done")
    mgr._sessions["s-ok"] = sess
    mgr._free_slot("s-ok")
    rt = mgr.status()["recent_terminal"]
    assert rt["s-ok"]["terminal_kind"] == "done"


def test_terminal_kind_crashed_plumbed_through_status():
    mgr = _mgr()
    sess = _FakeSession("s-cr", state="crashed", terminal_kind="crashed")
    mgr._sessions["s-cr"] = sess
    mgr._free_slot("s-cr")
    rt = mgr.status()["recent_terminal"]
    assert rt["s-cr"]["terminal_kind"] == "crashed"


def test_session_terminal_kind_set_by_fail_delivery(tmp_path, monkeypatch):
    """Session._terminal_kind is set to 'delivery_failed' when _fail_delivery is called."""
    from daemon.session import Session
    from daemon.config import ExecutorSpec

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class _Handle:
        def is_alive(self): return True
        def render(self): return ""
        def leader_pid(self): return None
        def leader_pgid(self): return None
        def pump(self, t): return False
        def flush_viewport(self, dialog): pass

    class _Drv:
        command_prefixes = ()
        submit_key = "\r"
        def __init__(self): self._settle = 0

    from daemon.events import EventQueue
    spec = ExecutorSpec(command="c", args=[], env={}, driver="claude")
    sess = Session("s-fd01", "claude", _Drv(), object(), spec, EventQueue())
    # Wire a live-ish handle + dialog so _fail_delivery can call flush_viewport + _publish.
    from daemon.dialog import Dialog
    sess._dialog = Dialog(tmp_path / "s-fd01", tail_lines=200, spool_max_bytes=0)
    sess._handle = _Handle()

    sess._fail_delivery("write_unconfirmed")
    assert sess._terminal_kind == "delivery_failed"
    assert sess.terminal_snapshot()["terminal_kind"] == "delivery_failed"
