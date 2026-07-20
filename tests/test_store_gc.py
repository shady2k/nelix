"""Tests for GC of durable rows + runtime dirs of retired generations (nelix-80e §3.7).

Real Store on temp dir, injected clock, real filesystem paths for runtime GC.
Follows the house style of test_store_terminal.py and test_store_identity.py.
"""
import json

import pytest

from nelix_store.gc import gc_runtime_dirs
from nelix_store.ledger import StartLedger
from nelix_store.store import Store

OID = "o-" + "2" * 32
GID1 = "g-" + "3" * 32
GID2 = "g-" + "4" * 32
GEPOCH1 = "g-" + "6" * 32
GEPOCH2 = "g-" + "7" * 32


class StepClock:
    """Clock that advances by N on each call, starting from INITIAL."""
    def __init__(self, initial=1000.0, step=0.0):
        self.t = initial
        self.step = step

    def __call__(self):
        v = self.t
        self.t += self.step
        return v

    def advance(self, delta):
        self.t += delta


@pytest.fixture
def clock():
    return StepClock(initial=500.0)


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path, clock=clock)
    yield s
    s.close()


@pytest.fixture
def ledger(tmp_path, clock):
    lg = StartLedger(tmp_path, clock=clock)
    yield lg
    lg.close()


def _make_retired(store, gen_id=GID1, epoch_id=GEPOCH1, build_id="b1",
                  final_hw=10, confirmed_hw=10):
    """Set up a generation as fully retired: lifecycle_state=retired,
    current_epoch=NULL, epoch certified, confirmed_high_water >= final_high_water."""
    store.create_generation(
        gen_id, build_id=build_id, lifecycle_state="ready",
        capability_snapshot=None, created_at=100.0)
    store.set_generation_lifecycle_state(gen_id, "active")
    store.insert_epoch(epoch_id, gen_id, incarnation_meta=None, created_at=101.0)
    store.set_epoch_process_state(epoch_id, "dead")
    store.set_epoch_retirement(epoch_id, retirement_state="certified",
                               certificate='{"ok":true}', final_high_water=final_hw)
    store.set_generation_confirmed_high_water(epoch_id, confirmed_hw)
    store.clear_current_epoch(gen_id)
    store.set_generation_lifecycle_state(gen_id, "retired")


def _started_session(store, ledger, gen_id=GID1, epoch_id=GEPOCH1,
                     owner="hermes:local", key="k1"):
    r = ledger.reserve(idempotency_key=key, owner_id=owner,
                       orchestration_id=OID, request_fingerprint="fp")
    ledger.assign_generation(r.session_id, gen_id, epoch_id)
    store.create_session(r.session_id, state="running", executor="coder",
                         task="t", cwd="/repo", model=None, created_at=100.0)
    return r.session_id


# ---- FK-order ----

def test_deleting_sessions_without_terminal_is_refused(store, ledger):
    _make_retired(store)
    sid = _started_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    conn = store._conn
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(Exception):
        conn.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
    conn.execute("ROLLBACK")


def test_gc_deletes_in_fk_order(store, ledger, clock):
    _make_retired(store)
    sid = _started_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    clock.advance(1)
    result = store.gc_retired_generations(replay_horizon_seconds=0)
    assert result["terminals_deleted"] == 1
    assert result["sessions_deleted"] == 1
    assert result["starts_deleted"] == 1
    assert store._conn.execute("SELECT 1 FROM terminal WHERE session_id=?", (sid,)).fetchone() is None
    assert store._conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone() is None
    assert store._conn.execute("SELECT 1 FROM starts WHERE session_id=?", (sid,)).fetchone() is None


# ---- only-retired ----

def test_gc_skips_live_generation(store, ledger):
    _make_retired(store, gen_id=GID1, epoch_id=GEPOCH1)
    store.create_generation(
        GID2, build_id="b2", lifecycle_state="active",
        capability_snapshot=None, created_at=200.0)
    store.insert_epoch(GEPOCH2, GID2, incarnation_meta=None, created_at=201.0)
    result = store.gc_retired_generations(replay_horizon_seconds=0)
    assert result["terminals_deleted"] == 0
    assert result["sessions_deleted"] == 0
    assert result["starts_deleted"] == 0


def test_gc_skips_draining_generation(store, ledger):
    store.create_generation(
        GID1, build_id="b1", lifecycle_state="draining",
        capability_snapshot=None, created_at=100.0)
    result = store.gc_retired_generations(replay_horizon_seconds=0)
    assert result["terminals_deleted"] == 0


def test_gc_skips_retiring_generation(store, ledger):
    store.create_generation(
        GID1, build_id="b1", lifecycle_state="retiring",
        capability_snapshot=None, created_at=100.0)
    result = store.gc_retired_generations(replay_horizon_seconds=0)
    assert result["terminals_deleted"] == 0


def test_gc_skips_generation_with_unresolved_terminal(store, ledger):
    _make_retired(store)
    sid = _started_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    result = store.gc_retired_generations(replay_horizon_seconds=86400)
    assert result["terminals_deleted"] == 0
    assert result["sessions_deleted"] == 0
    assert result["starts_deleted"] == 0


def test_gc_reclaims_fully_retired_generation(store, ledger, clock):
    _make_retired(store)
    sid = _started_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    clock.advance(1)
    result = store.gc_retired_generations(replay_horizon_seconds=0)
    assert result["terminals_deleted"] == 1
    assert result["sessions_deleted"] == 1
    assert result["starts_deleted"] == 1


def test_gc_reclaims_expired_terminal(store, ledger, clock):
    _make_retired(store)
    sid = _started_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    clock.advance(1)
    store.prune_terminal(max_age_seconds=0, max_count=100)
    result = store.gc_retired_generations(replay_horizon_seconds=86400)
    assert result["terminals_deleted"] == 1


# ---- replay horizon ----

def test_starts_within_replay_horizon_not_deleted(store, ledger):
    _make_retired(store)
    sid = _started_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    result = store.gc_retired_generations(replay_horizon_seconds=86400)
    assert result["terminals_deleted"] == 1
    assert result["sessions_deleted"] == 1
    assert result["starts_deleted"] == 0
    assert result["starts_preserved"] == 1


def test_starts_past_replay_horizon_deleted(store, ledger, clock):
    _make_retired(store)
    sid = _started_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    clock.advance(1)
    result = store.gc_retired_generations(replay_horizon_seconds=0)
    assert result["terminals_deleted"] == 1
    assert result["sessions_deleted"] == 1
    assert result["starts_deleted"] == 1
    assert result["starts_preserved"] == 0


def test_dedup_still_works_within_horizon(store, ledger):
    _make_retired(store)
    sid = _started_session(store, ledger, key="k_dedup")
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    result = store.gc_retired_generations(replay_horizon_seconds=86400)
    assert result["starts_deleted"] == 0
    assert result["starts_preserved"] == 1
    row = store._conn.execute(
        "SELECT 1 FROM starts WHERE session_id=?", (sid,)).fetchone()
    assert row is not None


# ---- idempotent GC ----

def test_gc_idempotent(store, ledger, clock):
    _make_retired(store)
    sid = _started_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    clock.advance(1)
    r1 = store.gc_retired_generations(replay_horizon_seconds=0)
    assert r1["terminals_deleted"] == 1
    r2 = store.gc_retired_generations(replay_horizon_seconds=0)
    assert r2["terminals_deleted"] == 0
    assert r2["sessions_deleted"] == 0
    assert r2["starts_deleted"] == 0


def test_gc_no_error_on_empty_store(store):
    result = store.gc_retired_generations(replay_horizon_seconds=0)
    assert result["terminals_deleted"] == 0
    assert result["sessions_deleted"] == 0


# ---- sessions durability (sessions row only deleted by GC) ----

def test_sessions_not_deleted_by_remove_live_session(store, ledger):
    _make_retired(store)
    sid = _started_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    row = store._conn.execute(
        "SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone()
    assert row is not None


# ---- runtime refcount ----

def _touch_runtime(rt_root, build_id):
    d = rt_root / build_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({"build_id": build_id, "core_version": "test"}))


def test_runtime_not_deleted_while_live_generation_references_build(store, tmp_path, monkeypatch):
    import nelix_store.gc as gc_mod
    monkeypatch.setattr(gc_mod.paths, "runtimes_root", lambda: tmp_path / "runtimes")
    rt_root = tmp_path / "runtimes"
    _touch_runtime(rt_root, "build_shared")

    store.create_generation(
        GID1, build_id="build_shared", lifecycle_state="active",
        capability_snapshot=None, created_at=100.0)
    _make_retired(store, gen_id=GID2, epoch_id=GEPOCH2, build_id="build_shared")

    result = gc_runtime_dirs(store)
    assert result["dirs_deleted"] == 0
    assert (rt_root / "build_shared").is_dir()


def test_runtime_deleted_when_no_non_retired_refs(store, tmp_path, monkeypatch):
    import nelix_store.gc as gc_mod
    monkeypatch.setattr(gc_mod.paths, "runtimes_root", lambda: tmp_path / "runtimes")
    rt_root = tmp_path / "runtimes"
    _touch_runtime(rt_root, "build_old")

    _make_retired(store, gen_id=GID1, epoch_id=GEPOCH1, build_id="build_old")

    result = gc_runtime_dirs(store)
    assert result["dirs_deleted"] == 1
    assert not (rt_root / "build_old").is_dir()


def test_runtime_skipped_when_install_lock_held(store, tmp_path, monkeypatch):
    import nelix_store.gc as gc_mod
    monkeypatch.setattr(gc_mod.paths, "runtimes_root", lambda: tmp_path / "runtimes")
    lock_path = gc_mod.paths.runtime_install_lock()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text('{"pid": 9999, "build": "build_locked"}')
    rt_root = tmp_path / "runtimes"
    _touch_runtime(rt_root, "build_locked")

    _make_retired(store, gen_id=GID1, epoch_id=GEPOCH1, build_id="build_locked")

    result = gc_runtime_dirs(store)
    assert result["dirs_deleted"] == 0
    assert result["dirs_skipped"] == ["build_locked"]


def test_runtime_not_deleted_when_retirement_not_confirmed(store, tmp_path, monkeypatch):
    import nelix_store.gc as gc_mod
    monkeypatch.setattr(gc_mod.paths, "runtimes_root", lambda: tmp_path / "runtimes")
    rt_root = tmp_path / "runtimes"
    _touch_runtime(rt_root, "build_unconfirmed")

    store.create_generation(
        GID1, build_id="build_unconfirmed", lifecycle_state="retired",
        capability_snapshot=None, created_at=100.0)
    store.insert_epoch(GEPOCH1, GID1, incarnation_meta=None, created_at=101.0)

    result = gc_runtime_dirs(store)
    assert result["dirs_deleted"] == 0


def test_runtime_not_deleted_when_oracle_clean_but_not_retired_gen_shares_build(
        store, tmp_path, monkeypatch):
    """The refcount guard, NOT the oracle, protects a build shared with a generation
    that is oracle-clean (no current epoch, every epoch certified, confirmed>=final)
    yet lifecycle_state != retired — the transient window during retire(). The oracle
    checks epochs/high-water but NOT lifecycle_state, so it returns no blockers here;
    only the ``any_non_retired`` refcount check keeps the shared runtime dir alive.
    """
    from nelix_contracts.retirement import generation_retirement_oracle_blockers
    import nelix_store.gc as gc_mod
    monkeypatch.setattr(gc_mod.paths, "runtimes_root", lambda: tmp_path / "runtimes")
    rt_root = tmp_path / "runtimes"
    _touch_runtime(rt_root, "build_shared")

    # Oracle-clean but lifecycle_state=active (NOT retired): certified dead epoch,
    # confirmed >= final, no current epoch — yet never flipped to "retired".
    store.create_generation(
        GID1, build_id="build_shared", lifecycle_state="ready",
        capability_snapshot=None, created_at=100.0)
    store.set_generation_lifecycle_state(GID1, "active")
    store.insert_epoch(GEPOCH1, GID1, incarnation_meta=None, created_at=101.0)
    store.set_epoch_process_state(GEPOCH1, "dead")
    store.set_epoch_retirement(GEPOCH1, retirement_state="certified",
                               certificate='{"ok":true}', final_high_water=10)
    store.set_generation_confirmed_high_water(GEPOCH1, 10)
    store.clear_current_epoch(GID1)
    # A retired peer sharing the same build.
    _make_retired(store, gen_id=GID2, epoch_id=GEPOCH2, build_id="build_shared")

    # Precondition that makes this test load-bearing for any_non_retired: the live gen
    # IS oracle-clean, so oracle-confirm alone would NOT stop the delete.
    assert generation_retirement_oracle_blockers(
        store=store, generation_id=GID1) == ()
    assert store.get_generation(GID1).lifecycle_state != "retired"

    result = gc_runtime_dirs(store)
    assert result["dirs_deleted"] == 0
    assert (rt_root / "build_shared").is_dir()
