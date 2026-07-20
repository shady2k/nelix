import json
import paths
import pytest
from daemon.events import EventQueue
from daemon.manager import SessionManager
from daemon.config import ExecutorSpec
from tests.conftest import OWNER, reserve_start


class _FakeSession:
    instances = []
    def __init__(self, sid, executor, spec):
        self._id = sid; self._executor = executor; self._spec = spec
        self.on_terminal = None; self.reaper_ctx = None
        self.lineage_id = None; self.restarted_from = None; self.restart_count = 0
        self._task = None; self._cwd = None; self.stopped = False; self.started = False
        _FakeSession.instances.append(self)
    @property
    def executor(self): return self._executor
    @property
    def task(self): return self._task
    @property
    def cwd(self): return self._cwd
    def start(self, task, cwd):
        self._task = task; self._cwd = cwd; self.started = True
    def stop(self): self.stopped = True
    def observe(self): pass
    def last_observed(self): return 0.0
    def orphan_marked_ts(self): return None
    def mark_orphaned(self, grace): pass
    def snapshot(self): return {"session_id": self._id}
    def terminal_snapshot(self):
        return {"session_id": self._id, "terminal": True, "state": "crashed"}


def _mgr(tmp_path, monkeypatch, store_and_ledger, limit=2, max_restarts=3):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    _FakeSession.instances = []
    store, ledger = store_and_ledger
    spec = ExecutorSpec(command="c", args=[], env={}, driver="claude", max_restarts=max_restarts)
    mgr = SessionManager({"claude": spec}, EventQueue(), store, concurrency_limit=limit,
                          session_factory=lambda sid, ex, sp, ev: _FakeSession(sid, ex, sp),
                          session_retain=0, session_max_age_days=0)
    return mgr, ledger


def test_restart_active_session_replaces_at_full_capacity(tmp_path, monkeypatch, store_and_ledger):
    mgr, ledger = _mgr(tmp_path, monkeypatch, store_and_ledger, limit=1)
    _out = mgr.start("claude", "task A", str(tmp_path), owner_id=OWNER,
                     session_id=reserve_start(ledger)); sid = _out.session_id
    out = mgr.restart(sid, new_session_id=reserve_start(ledger), owner_id=OWNER)  # at full cap (limit 1)
    assert out.status == "restarted"
    assert out.session_id != sid                            # new id
    assert out.lineage_id == sid                            # lineage = first session id
    new = mgr.get(out.session_id)
    assert new.restarted_from == sid and new.task == "task A"
    old = next(s for s in _FakeSession.instances if s._id == sid)
    assert old.stopped is True


def test_restart_gone_session_resolves_from_persisted_meta(tmp_path, monkeypatch, store_and_ledger):
    mgr, ledger = _mgr(tmp_path, monkeypatch, store_and_ledger, limit=2)
    _out = mgr.start("claude", "task B", str(tmp_path), owner_id=OWNER,
                     session_id=reserve_start(ledger)); sid = _out.session_id
    # Simulate a crash: write meta to disk (Task 2 does this in real start) and free the slot.
    paths.ensure_private_dir(paths.sessions_root() / sid)
    paths.session_meta(paths.sessions_root() / sid).write_text(json.dumps(
        {"executor": "claude", "task": "task B", "cwd": str(tmp_path),
         "lineage_id": sid, "restarted_from": None}))
    mgr._free_slot(sid)                                     # session gone from _sessions
    out = mgr.restart(sid, new_session_id=reserve_start(ledger), owner_id=OWNER)
    assert out.status == "restarted"
    assert mgr.get(out.session_id).task == "task B"


def test_restart_unknown_session(tmp_path, monkeypatch, store_and_ledger):
    mgr, ledger = _mgr(tmp_path, monkeypatch, store_and_ledger)
    assert mgr.restart("s-nope", new_session_id=reserve_start(ledger), owner_id=OWNER).status == "unknown_session"


def test_restart_budget_exhausted_then_force_resets(tmp_path, monkeypatch, store_and_ledger):
    mgr, ledger = _mgr(tmp_path, monkeypatch, store_and_ledger, limit=1, max_restarts=2)
    _out = mgr.start("claude", "t", str(tmp_path), owner_id=OWNER,
                     session_id=reserve_start(ledger)); sid = _out.session_id
    o1 = mgr.restart(sid, new_session_id=reserve_start(ledger), owner_id=OWNER); assert o1.status == "restarted" and o1.restart_count == 1
    o2 = mgr.restart(o1.session_id, new_session_id=reserve_start(ledger), owner_id=OWNER); assert o2.status == "restarted" and o2.restart_count == 2
    o3 = mgr.restart(o2.session_id, new_session_id=reserve_start(ledger), owner_id=OWNER)
    assert o3.status == "restart_budget_exhausted" and o3.max_restarts == 2
    o4 = mgr.restart(o2.session_id, new_session_id=reserve_start(ledger), force=True, owner_id=OWNER)
    assert o4.status == "restarted" and o4.restart_count == 1   # reset then +1


def test_restart_budget_is_per_lineage(tmp_path, monkeypatch, store_and_ledger):
    mgr, ledger = _mgr(tmp_path, monkeypatch, store_and_ledger, limit=2, max_restarts=1)
    _outa = mgr.start("claude", "A", str(tmp_path), owner_id=OWNER,
                      session_id=reserve_start(ledger)); a = _outa.session_id
    _outb = mgr.start("claude", "B", str(tmp_path), owner_id=OWNER,
                      session_id=reserve_start(ledger)); b = _outb.session_id
    oa = mgr.restart(a, new_session_id=reserve_start(ledger), owner_id=OWNER); assert oa.status == "restarted"        # lineage A: count 1
    ob = mgr.restart(b, new_session_id=reserve_start(ledger), owner_id=OWNER); assert ob.status == "restarted"        # lineage B independent: count 1
    assert mgr.restart(oa.session_id, new_session_id=reserve_start(ledger), owner_id=OWNER).status == "restart_budget_exhausted"
    assert mgr.restart(ob.session_id, new_session_id=reserve_start(ledger), owner_id=OWNER).status == "restart_budget_exhausted"


def test_restart_does_not_hold_lock_across_stop(tmp_path, monkeypatch, store_and_ledger):
    # A stop() that re-enters the manager lock (real Session._free_slot does) must not deadlock.
    mgr, ledger = _mgr(tmp_path, monkeypatch, store_and_ledger, limit=1)
    _out = mgr.start("claude", "t", str(tmp_path), owner_id=OWNER,
                     session_id=reserve_start(ledger)); sid = _out.session_id
    src = mgr.get(sid)
    def reentrant_stop():
        mgr._free_slot(sid)         # takes mgr._lock — would deadlock if restart held it across stop
        src.stopped = True
    src.stop = reentrant_stop
    out = mgr.restart(sid, new_session_id=reserve_start(ledger), owner_id=OWNER)
    assert out.status == "restarted" and src.stopped is True


def test_restart_outcome_carries_next_after_seq(tmp_path, monkeypatch, store_and_ledger):
    # The restarted session must report its base seq so the plugin can arm a waiter at exactly the
    # right cursor (symmetric with /start's next_after_seq), not blindly from 0.
    mgr, ledger = _mgr(tmp_path, monkeypatch, store_and_ledger, limit=1)
    _out = mgr.start("claude", "task A", str(tmp_path), owner_id=OWNER,
                     session_id=reserve_start(ledger)); sid = _out.session_id
    mgr._events.publish(sid, "claude", "waiting_for_user", "?", "idle_prompt")  # bump the high-water
    out = mgr.restart(sid, new_session_id=reserve_start(ledger), owner_id=OWNER)
    assert out.status == "restarted"
    assert isinstance(out.next_after_seq, int)
    assert out.next_after_seq == mgr._events.latest_seq()   # base seq = high-water at the new spawn


def test_restart_balances_reserved_counter(tmp_path, monkeypatch, store_and_ledger):
    # The reservation must return to 0 after a restart (active and already-gone) so it never leaks
    # nor overcounts capacity for concurrent starts. (Guards the overcount/leak fix in _spawn.)
    mgr, ledger = _mgr(tmp_path, monkeypatch, store_and_ledger, limit=1)
    _out = mgr.start("claude", "t", str(tmp_path), owner_id=OWNER,
                     session_id=reserve_start(ledger)); sid = _out.session_id
    out = mgr.restart(sid, new_session_id=reserve_start(ledger), owner_id=OWNER)  # active path (reserve)
    assert out.status == "restarted" and mgr._reserved == 0
    # Now a single free slot exists for the lineage; a fresh start at limit 1 must be rejected,
    # proving the restart did not leave the cap overcounted or undercounted.
    with pytest.raises(RuntimeError, match="concurrency_limit=1 reached"):
        mgr.start("claude", "other", str(tmp_path), owner_id=OWNER,
                  session_id=reserve_start(ledger))
