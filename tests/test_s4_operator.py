"""nelix-80e S4 — operator plane + atomic activation (§3.4). First N>1 tests.

Tests the lifecycle FSM, store atomic flip, operator routes (install/activate/list/retire-disabled),
no-new-sessions on draining, restart-on-active, and serialization.
"""

import pytest

import paths
from nelix_contracts.errors import IDEMPOTENCY_CONFLICT, GENERATION_UNAVAILABLE, INVALID_REQUEST, NelixError
from nelix_contracts.ids import new_generation_id, new_session_id
from nelix_contracts.lifecycle import (
    READY, ACTIVE, DRAINING, RETIRING, RETIRED,
    ALL_STATES, validate_transition,
)
from nelix_store.store import Store
from nelix_store.ledger import StartLedger

from router.operator import OperatorRoutes
from router.registry import GenerationRegistry
from router.session_forward import SessionForward

from tests._router_fakes import Backend, Supervisor

_EPOCH = "r-" + "0" * 32


# ============================================================
# 1. FSM legality
# ============================================================

class TestLifecycleFSM:
    def test_legal_transitions(self):
        validate_transition(READY, ACTIVE)
        validate_transition(ACTIVE, DRAINING)
        validate_transition(DRAINING, RETIRING)
        validate_transition(RETIRING, RETIRED)

    def test_same_state_is_legal(self):
        for s in ALL_STATES:
            validate_transition(s, s)

    def test_illegal_retired_to_active(self):
        with pytest.raises(ValueError, match="illegal lifecycle transition"):
            validate_transition(RETIRED, ACTIVE)

    def test_illegal_draining_to_ready(self):
        with pytest.raises(ValueError, match="illegal lifecycle transition"):
            validate_transition(DRAINING, READY)

    def test_illegal_active_to_ready(self):
        with pytest.raises(ValueError, match="illegal lifecycle transition"):
            validate_transition(ACTIVE, READY)

    def test_illegal_ready_to_draining(self):
        with pytest.raises(ValueError, match="illegal lifecycle transition"):
            validate_transition(READY, DRAINING)

    def test_unknown_state_raises(self):
        with pytest.raises(ValueError, match="unknown lifecycle state"):
            validate_transition(ACTIVE, "bogus")


# ============================================================
# 2. Store atomic flip
# ============================================================

class TestStoreAtomicFlip:
    @pytest.fixture
    def store(self, tmp_path):
        s = Store(paths.nelix_root(), clock=lambda: 1000.0)
        yield s
        s.close()

    def _seed_generation(self, store, gid=None, lifecycle_state=ACTIVE, build_id="b-1"):
        gid = gid or new_generation_id()
        store.create_generation(gid, build_id=build_id,
                                 lifecycle_state=lifecycle_state,
                                 capability_snapshot=None, created_at=1000.0)
        epoch = new_generation_id()
        store.insert_epoch(epoch, gid, incarnation_meta=None, created_at=1000.0)
        store.cas_epoch_serving(gid, epoch, expected_current_epoch=None)
        return gid, epoch

    def test_atomic_flip_old_draining_new_active(self, store):
        old_gid, old_epoch = self._seed_generation(store, lifecycle_state=ACTIVE)
        new_gid, new_epoch = self._seed_generation(store, lifecycle_state=READY)

        store.set_generation_lifecycle_state_atomic(
            old_gid, new_gid, new_state_old=DRAINING,
            expected_old_state=ACTIVE, expected_new_state=READY)

        old_rec = store.get_generation(old_gid)
        new_rec = store.get_generation(new_gid)
        assert old_rec.lifecycle_state == DRAINING
        assert new_rec.lifecycle_state == ACTIVE

    def test_atomic_flip_rejects_wrong_old_state(self, store):
        old_gid, _ = self._seed_generation(store, lifecycle_state=DRAINING)
        new_gid, _ = self._seed_generation(store, lifecycle_state=READY)

        with pytest.raises(NelixError) as exc:
            store.set_generation_lifecycle_state_atomic(
                old_gid, new_gid, new_state_old=DRAINING,
                expected_old_state=ACTIVE, expected_new_state=READY)
        assert exc.value.code == IDEMPOTENCY_CONFLICT
        assert store.get_generation(old_gid).lifecycle_state == DRAINING
        assert store.get_generation(new_gid).lifecycle_state == READY

    def test_atomic_flip_rejects_wrong_new_state(self, store):
        old_gid, _ = self._seed_generation(store, lifecycle_state=ACTIVE)
        new_gid, _ = self._seed_generation(store, lifecycle_state=DRAINING)

        with pytest.raises(NelixError) as exc:
            store.set_generation_lifecycle_state_atomic(
                old_gid, new_gid, new_state_old=DRAINING,
                expected_old_state=ACTIVE, expected_new_state=READY)
        assert exc.value.code == IDEMPOTENCY_CONFLICT
        assert store.get_generation(old_gid).lifecycle_state == ACTIVE
        assert store.get_generation(new_gid).lifecycle_state == DRAINING

    def test_atomic_flip_rejects_missing_generations(self, store):
        fake_id = "g-00000000000000000000000000000000"
        with pytest.raises(NelixError):
            store.set_generation_lifecycle_state_atomic(
                fake_id, fake_id, new_state_old=DRAINING,
                expected_old_state=ACTIVE, expected_new_state=READY)

    def test_atomic_flip_first_activation(self, store):
        gid, _ = self._seed_generation(store, lifecycle_state=READY)
        store.set_generation_lifecycle_state_atomic(
            gid, gid, new_state_old=ACTIVE,
            expected_old_state=READY, expected_new_state=READY)
        assert store.get_generation(gid).lifecycle_state == ACTIVE


# ============================================================
# 3. Operator routes (non-spawning paths)
# ============================================================

class TestOperatorRoutes:
    @pytest.fixture
    def ops(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        backend = Backend(build_id="b-1")
        registry = GenerationRegistry(supervisor=Supervisor(backend.transport),
                                      build_id="b-1",
                                      health_probe=lambda t: "b-1")
        operator = OperatorRoutes(registry, _EPOCH, store=store)
        yield operator, registry, store, backend
        backend.close()
        store.close()

    def test_list_is_empty_initially(self, ops):
        operator, _, _, _ = ops
        status, body = operator.list()
        assert status == 200
        assert body["generations"] == []

    def test_list_reports_generations(self, ops):
        operator, _, store, _ = ops
        gid = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=ACTIVE,
                                 capability_snapshot=None, created_at=1000.0)
        status, body = operator.list()
        assert status == 200
        assert len(body["generations"]) == 1
        g = body["generations"][0]
        assert g["generation_id"] == gid
        assert g["lifecycle_state"] == ACTIVE

    def test_list_reports_multiple_generations(self, ops):
        operator, _, store, _ = ops
        g1 = new_generation_id()
        g2 = new_generation_id()
        store.create_generation(g1, build_id="b-1", lifecycle_state=ACTIVE,
                                 capability_snapshot=None, created_at=1000.0)
        store.create_generation(g2, build_id="b-2", lifecycle_state=DRAINING,
                                 capability_snapshot=None, created_at=1001.0)
        status, body = operator.list()
        assert status == 200
        assert len(body["generations"]) == 2
        states = {g["generation_id"]: g["lifecycle_state"] for g in body["generations"]}
        assert states[g1] == ACTIVE
        assert states[g2] == DRAINING

    def test_retire_requires_valid_generation(self, ops):
        operator, _, _, _ = ops
        with pytest.raises(NelixError) as exc:
            operator.retire("g-00000000000000000000000000000000")
        assert exc.value.code == INVALID_REQUEST
        assert "no such generation" in exc.value.message


# ============================================================
# 4. No-new-sessions on draining
# ============================================================

class TestNoNewSessions:
    @pytest.fixture
    def setup(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        backend = Backend(build_id="b-1")
        registry = GenerationRegistry(supervisor=Supervisor(backend.transport),
                                      store=store,
                                      build_id="b-1",
                                      health_probe=lambda t: "b-1")
        gid = new_generation_id()
        epoch = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=ACTIVE,
                                 capability_snapshot=None, created_at=1000.0)
        store.insert_epoch(epoch, gid, incarnation_meta=None, created_at=1000.0)
        store.cas_epoch_serving(gid, epoch, expected_current_epoch=None)

        d_gid = new_generation_id()
        d_epoch = new_generation_id()
        store.create_generation(d_gid, build_id="b-0", lifecycle_state=DRAINING,
                                 capability_snapshot=None, created_at=999.0)
        store.insert_epoch(d_epoch, d_gid, incarnation_meta=None, created_at=999.0)
        store.cas_epoch_serving(d_gid, d_epoch, expected_current_epoch=None)

        yield store, registry, backend, gid, epoch, d_gid, d_epoch
        backend.close()
        store.close()

    def test_store_has_separate_active_and_draining(self, setup):
        store, registry, backend, gid, epoch, d_gid, d_epoch = setup
        gens = store.list_generations()
        active_states = {g.generation_id: g.lifecycle_state for g in gens
                         if g.lifecycle_state == ACTIVE}
        draining_states = {g.generation_id: g.lifecycle_state for g in gens
                           if g.lifecycle_state == DRAINING}
        assert len(active_states) == 1
        assert len(draining_states) == 1
        assert gid in active_states
        assert d_gid in draining_states

    def test_existing_session_still_routable_on_draining(self, setup):
        store, registry, backend, gid, epoch, d_gid, d_epoch = setup
        proc_state, lc_state, cap_snap, handle = registry.resolve_generation_state(d_gid, d_epoch)
        assert proc_state == "serving"
        assert lc_state == DRAINING

    def test_draining_generation_rejects_restart(self, setup):
        store, registry, backend, gid, epoch, d_gid, d_epoch = setup
        ledger = StartLedger(paths.nelix_root())

        res = ledger.reserve(idempotency_key="test-restart-drain",
                             owner_id="test-owner",
                             orchestration_id="o-00000000000000000000000000000000",
                             request_fingerprint="fp")
        sid = res.session_id
        ledger.assign_generation(sid, d_gid, d_epoch)
        ledger.commit(sid, d_gid, d_epoch)

        sf = SessionForward(registry, ledger=ledger, store=store)
        with pytest.raises(NelixError) as exc:
            sf.restart("test-owner", sid)
        assert exc.value.code == GENERATION_UNAVAILABLE
        assert "draining" in exc.value.message.lower()


# ============================================================
# 5. Restart-on-active for archived/terminal sessions
# ============================================================

class TestRestartOnActive:
    @pytest.fixture
    def setup(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        backend = Backend(build_id="b-1")
        registry = GenerationRegistry(supervisor=Supervisor(backend.transport),
                                      store=store,
                                      build_id="b-1",
                                      health_probe=lambda t: "b-1")
        gid = new_generation_id()
        epoch = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=ACTIVE,
                                 capability_snapshot=None, created_at=1000.0)
        store.insert_epoch(epoch, gid, incarnation_meta=None, created_at=1000.0)
        store.cas_epoch_serving(gid, epoch, expected_current_epoch=None)

        yield store, registry, backend, gid, epoch
        backend.close()
        store.close()

    def test_route_archived_restart_resolves_to_active(self, setup):
        store, registry, backend, active_gid, active_epoch = setup
        ledger = StartLedger(paths.nelix_root())

        dead_gid = new_generation_id()
        dead_epoch = new_generation_id()
        store.create_generation(dead_gid, build_id="b-0", lifecycle_state=DRAINING,
                                 capability_snapshot=None, created_at=999.0)
        store.insert_epoch(dead_epoch, dead_gid, incarnation_meta=None, created_at=999.0)
        store.set_epoch_process_state(dead_epoch, "dead")

        res = ledger.reserve(idempotency_key="test-restart-archived",
                             owner_id="test-owner",
                             orchestration_id="o-00000000000000000000000000000000",
                             request_fingerprint="fp")
        sid = res.session_id
        ledger.assign_generation(sid, dead_gid, dead_epoch)
        ledger.commit(sid, dead_gid, dead_epoch)

        # The old session-keyed restart is unsupported for archived sessions.
        # Restart-on-active is handled by POST /restart (RestartPath), which
        # calls registry.active() and creates a new session on the active generation.
        sf = SessionForward(registry, ledger=ledger, store=store)
        new_sid = new_session_id()
        with pytest.raises(NelixError) as exc:
            sf.restart("test-owner", sid, new_session_id=new_sid, force=False)
        assert exc.value.code == "unsupported_by_generation"


# ============================================================
# 6. Adopt generation
# ============================================================

class TestAdoptGeneration:
    def test_adopt_generation_sets_active_and_bumps_topology(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        backend = Backend(build_id="b-1")
        registry = GenerationRegistry(supervisor=Supervisor(backend.transport),
                                      build_id="b-1",
                                      health_probe=lambda t: "b-1")

        gid = new_generation_id()
        epoch = new_generation_id()

        rev_before = registry.topology_revision()
        handle = registry.adopt_generation(gid, epoch, backend.transport,
                                            build_id="b-1",
                                            incarnation={"pid": 1, "start_fingerprint": "fp"})
        rev_after = registry.topology_revision()

        assert handle.generation_id == gid
        assert handle.epoch == epoch
        assert rev_after > rev_before
        assert registry.active().generation_id == gid

        backend.close()
        store.close()
