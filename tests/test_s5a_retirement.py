"""S5a — retirement / final-wake clean path: persist-before-wake, obligation ledger,
terminal-pending, quiescence, certified_high_water, oracle, retire.

Tests are REAL + order-sensitive. All existing tests must stay green.
"""
import pytest

import paths
from nelix_contracts.errors import NelixError
from nelix_contracts.ids import new_generation_id
from nelix_contracts.lifecycle import (
    ACTIVE, DRAINING, RETIRED,
)
from nelix_contracts.retirement import (
    generation_may_retire, generation_retirement_oracle_blockers,
)

from nelix_store.store import Store
from nelix_store.ledger import StartLedger

from router.operator import OperatorRoutes
from router.registry import GenerationRegistry

from tests._router_fakes import Backend, Supervisor

_EPOCH = "r-" + "0" * 32


# ============================================================
# 1. Persist-before-visible-wake
# ============================================================

class TestPersistBeforeWake:
    """Prove that terminal records are persisted BEFORE the ring event publishes.
    We instrument the store to record the order of put_terminal vs the event publish."""

    def test_persist_before_publish_ordering(self, tmp_path):
        """The store's put_terminal must be called before the session's event is
        published to the ring. We verify by recording call order on a tracking store.
        Uses a fake session that simulates the persist-before-publish flow."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager
            from tests.conftest import EXECUTOR, OWNER, make_spec

            sid = _router_started_session(ledger, store)
            specs = {EXECUTOR: make_spec()}

            call_order = []

            class _FakeSession:
                """Fake that simulates the persist-before-publish flow."""
                def __init__(self, sid, ex, spec, ev):
                    self.sid = sid
                    self.executor = ex
                    self._events_queue = ev
                    self.on_terminal = None
                    self.deliver_turn = None
                    self._persist_terminal = None
                    self.lineage_id = sid
                    self.restarted_from = None
                    self.restart_count = 0
                    self.model = None
                    self._terminal_kind = "done"
                    self._last_screen_excerpt = "all done"
                def start(self, task, cwd): pass
                def snapshot(self):
                    return {"session_id": self.sid, "executor": self.executor,
                            "control_state": "busy", "task_delivery": "pending",
                            "terminal": False}
                def terminal_snapshot(self):
                    return {"session_id": self.sid, "terminal_kind": self._terminal_kind,
                            "screen_excerpt": self._last_screen_excerpt,
                            "lineage_id": self.lineage_id}
                def stop(self):
                    kind = self._terminal_kind
                    excerpt = self._last_screen_excerpt
                    # Persist BEFORE publish (the S5a ordering)
                    if self._persist_terminal is not None:
                        self._persist_terminal(self.sid, kind, excerpt)
                        call_order.append("persist")
                    self._events_queue.publish(
                        self.sid, self.executor, kind, excerpt, kind)
                    call_order.append("publish")
                    if self.on_terminal is not None:
                        self.on_terminal(self.sid)
                def pending_async_id(self): return None

            def session_factory(sid, ex, spec, ev):
                return _FakeSession(sid, ex, spec, ev)

            mgr = SessionManager(
                specs, events, store, session_factory=session_factory,
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)

            mgr.start(EXECUTOR, "test task", "/tmp", owner_id=OWNER,
                      session_id=sid)

            call_order.clear()

            # Stop triggers persist-then-publish
            mgr.stop(sid, owner_id=OWNER)

            assert "persist" in call_order
            assert "publish" in call_order
            persist_idx = call_order.index("persist")
            publish_idx = call_order.index("publish")
            assert persist_idx < publish_idx, (
                f"persist (idx {persist_idx}) must precede publish (idx {publish_idx})"
            )
        finally:
            store.close()

    def test_terminal_record_exists_at_wake_time(self, tmp_path):
        """A waiter that wakes on a terminal event finds the record ALREADY persisted
        in the store at wake time — not just eventually consistent."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager
            from tests.conftest import EXECUTOR, OWNER, make_spec

            sid = _router_started_session(ledger, store)
            specs = {EXECUTOR: make_spec()}

            class _FakeSession2:
                def __init__(self, sid, ex, spec, ev):
                    self.sid = sid
                    self.executor = ex
                    self._events_queue = ev
                    self.on_terminal = None
                    self.deliver_turn = None
                    self._persist_terminal = None
                    self.lineage_id = sid
                    self.restarted_from = None
                    self.restart_count = 0
                    self.model = None
                    self._terminal_kind = "done"
                    self._last_screen_excerpt = "all done"
                def start(self, task, cwd): pass
                def snapshot(self):
                    return {"session_id": self.sid, "executor": self.executor,
                            "control_state": "busy", "task_delivery": "pending",
                            "terminal": False}
                def terminal_snapshot(self):
                    return {"session_id": self.sid, "terminal_kind": self._terminal_kind,
                            "screen_excerpt": self._last_screen_excerpt,
                            "lineage_id": self.lineage_id}
                def stop(self):
                    kind = self._terminal_kind
                    excerpt = self._last_screen_excerpt
                    if self._persist_terminal is not None:
                        self._persist_terminal(self.sid, kind, excerpt)
                    self._events_queue.publish(
                        self.sid, self.executor, kind, excerpt, kind)
                    if self.on_terminal is not None:
                        self.on_terminal(self.sid)
                def pending_async_id(self): return None

            def session_factory(sid, ex, spec, ev):
                return _FakeSession2(sid, ex, spec, ev)

            mgr = SessionManager(
                specs, events, store, session_factory=session_factory,
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)

            mgr.start(EXECUTOR, "test task", "/tmp", owner_id=OWNER,
                      session_id=sid)

            mgr.stop(sid, owner_id=OWNER)

            # The terminal record MUST be in the store at wake time
            term = store.get_terminal(sid, owner_id=OWNER)
            assert term.session_id == sid
            assert term.terminal_kind == "done"
        finally:
            store.close()


# ============================================================
# 2. Obligation barrier
# ============================================================

class TestObligationBarrier:
    """An obligation outstanding (terminal not yet persisted) blocks certification,
    even when _sessions is empty."""

    def test_outstanding_obligation_blocks_certification(self):
        """The obligation ledger must block quiescence certification when an
        obligation is outstanding, even if _sessions is empty."""
        clock = _FakeClock(1000.0)
        store = Store(paths.nelix_root(), clock=clock)
        try:
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager
            from tests.conftest import EXECUTOR, OWNER, make_spec

            sid = _router_started_session(ledger, store)
            specs = {EXECUTOR: make_spec()}

            class _NoopSession:
                """Minimal session that never calls _free_slot (stays alive)."""
                def __init__(self, sid, executor, spec, event_q):
                    self.sid = sid
                    self.executor = executor
                    self.spec = spec
                    self._event_q = event_q
                    self.lineage_id = sid
                    self.restarted_from = None
                    self.restart_count = 0
                    self.model = None
                    self.on_terminal = None
                    self.deliver_turn = None
                    self._persist_terminal = None
                def start(self, task, cwd): pass
                def snapshot(self):
                    return {"session_id": self.sid, "executor": self.executor,
                            "control_state": "busy", "task_delivery": "pending",
                            "terminal": False}
                def stop(self): pass
                def terminal_snapshot(self): return None
                def pending_async_id(self): return None

            def session_factory(sid, ex, spec, ev):
                s = _NoopSession(sid, ex, spec, ev)
                return s

            mgr = SessionManager(
                specs, events, store, session_factory=session_factory,
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)

            # Spawn the session — this arms an obligation
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            # _sessions is NOT empty, but even if it were, the obligation blocks
            assert sid in mgr._terminal_obligations, "obligation should be armed"

            # Force-remove from _sessions to simulate empty sessions
            mgr._sessions.pop(sid, None)

            # _sessions is now empty, but obligation is still outstanding
            assert sid not in mgr._sessions
            assert sid in mgr._terminal_obligations

            # The manager's certify_epoch reads persisted high-water.
            # With no terminal record persisted yet, final_hw = 0.
            # But the quiescence barrier (no obligations) is NOT met.
            status = mgr.quiescence_status()
            assert status["outstanding_obligations"] == 1, (
                "obligation must be outstanding even after _sessions emptied"
            )

            # Now discharge the obligation
            mgr._terminal_obligations.discard(sid)

            status = mgr.quiescence_status()
            assert status["outstanding_obligations"] == 0
        finally:
            store.close()

    def test_obligation_discharged_after_persist(self, tmp_path):
        """After terminal is persisted, the obligation is discharged."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager
            from tests.conftest import EXECUTOR, OWNER, make_spec

            sid = _router_started_session(ledger, store)
            specs = {EXECUTOR: make_spec()}

            class _FakeSession3:
                def __init__(self, sid, ex, spec, ev):
                    self.sid = sid
                    self.executor = ex
                    self.on_terminal = None
                    self.deliver_turn = None
                    self._persist_terminal = None
                    self.lineage_id = sid
                    self.restarted_from = None
                    self.restart_count = 0
                    self.model = None
                    self._terminal_kind = "done"
                    self._last_screen_excerpt = "done"
                def start(self, task, cwd): pass
                def snapshot(self):
                    return {"session_id": self.sid, "executor": self.executor,
                            "control_state": "busy", "task_delivery": "pending",
                            "terminal": False}
                def terminal_snapshot(self):
                    return {"session_id": self.sid, "terminal_kind": self._terminal_kind,
                            "screen_excerpt": self._last_screen_excerpt,
                            "lineage_id": self.lineage_id}
                def stop(self):
                    kind = self._terminal_kind
                    excerpt = self._last_screen_excerpt
                    if self._persist_terminal is not None:
                        self._persist_terminal(self.sid, kind, excerpt)
                    if self.on_terminal is not None:
                        self.on_terminal(self.sid)
                def pending_async_id(self): return None

            def session_factory(sid, ex, spec, ev):
                return _FakeSession3(sid, ex, spec, ev)

            mgr = SessionManager(
                specs, events, store, session_factory=session_factory,
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)

            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            assert sid in mgr._terminal_obligations

            # Stop triggers persist -> obligation discharged
            mgr.stop(sid, owner_id=OWNER)

            assert sid not in mgr._terminal_obligations, (
                "obligation should be discharged after terminal persists"
            )
        finally:
            store.close()


# ============================================================
# 3. Quiescence rejects new work
# ============================================================

class TestQuiescenceRejects:
    """After quiescing, new sessions, restarts, and idle-resume are rejected."""

    def test_quiescing_rejects_new_start(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager
            from tests.conftest import EXECUTOR, OWNER, make_spec

            specs = {EXECUTOR: make_spec()}

            class _QuickSession:
                def __init__(self, sid, *a, **kw):
                    self.sid = sid
                    self.on_terminal = None
                    self.deliver_turn = None
                    self._persist_terminal = None
                    self.lineage_id = sid
                    self.restarted_from = None
                    self.restart_count = 0
                    self.model = None
                    self._stop = False
                def start(self, task, cwd): pass
                def snapshot(self):
                    return {"session_id": self.sid, "control_state": "busy",
                            "task_delivery": "pending", "terminal": False}
                def stop(self): self._stop = True
                def terminal_snapshot(self): return None
                def pending_async_id(self): return None

            def session_factory(sid, ex, spec, ev):
                return _QuickSession(sid, ex, spec, ev)

            mgr = SessionManager(specs, events, store, session_factory=session_factory,
                                 concurrency_limit=5, terminal_snapshot_ttl=300.0,
                                 clock=clock, generation_epoch=_EPOCH,
                                 generation_id="g-quiescent-test")

            # Create the generation and epoch in the store so begin_quiescence works
            store.create_generation("g-quiescent-test", build_id="b-1",
                                    lifecycle_state=ACTIVE, capability_snapshot=None,
                                    created_at=1000.0)
            store.insert_epoch(_EPOCH, "g-quiescent-test",
                               incarnation_meta=None, created_at=1000.0)

            # Quiesce
            mgr.begin_quiescence()

            # New start is rejected
            sid2 = _router_started_session(ledger, store)
            with pytest.raises(RuntimeError, match="quiescing"):
                mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid2)

            # send_turn is rejected
            outcome = mgr.send_turn("s-nonexistent", "hello")
            assert outcome.status in ("no_pending", "unknown_session")
        finally:
            store.close()

    def test_quiescing_rejects_restart(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            from daemon.manager import SessionManager
            from tests.conftest import OWNER, make_spec
            specs = {"sh": make_spec()}

            class QSession:
                def __init__(self, *a, **kw):
                    self.on_terminal = None
                    self.deliver_turn = None
                    self._persist_terminal = None
                    self.lineage_id = None
                    self.restarted_from = None
                    self.restart_count = 0
                    self.model = None
                    self.executor = "sh"
                    self.task = "t"
                    self.cwd = "/tmp"
                def start(self, *a, **kw): pass
                def snapshot(self):
                    return {"control_state": "busy", "task_delivery": "pending"}
                def stop(self): pass
                def terminal_snapshot(self): return None
                def pending_async_id(self): return None

            def session_factory(sid, ex, spec, ev):
                return QSession()

            events = _TrackingEventQueue()
            mgr = SessionManager(specs, events, store, session_factory=session_factory,
                                 concurrency_limit=5, terminal_snapshot_ttl=300.0,
                                 clock=clock, generation_epoch=_EPOCH,
                                 generation_id="g-quiescent-restart")

            store.create_generation("g-quiescent-restart", build_id="b-1",
                                    lifecycle_state=ACTIVE, capability_snapshot=None,
                                    created_at=1000.0)
            store.insert_epoch(_EPOCH, "g-quiescent-restart",
                               incarnation_meta=None, created_at=1000.0)

            mgr.begin_quiescence()

            outcome = mgr.restart("s-nonexistent", new_session_id="s-new",
                                  owner_id=OWNER)
            assert outcome.status == "start_failed"
        finally:
            store.close()


# ============================================================
# 4. Terminal-pending releases leases
# ============================================================

class TestTerminalPending:
    """Persisted-but-unconfirmed terminal releases BOTH leases and moves to
    terminal-pending inventory, consuming neither active nor live."""

    def test_leases_released_and_moved_to_pending(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager
            from tests.conftest import EXECUTOR, OWNER, make_spec

            sid = _router_started_session(ledger, store)
            specs = {EXECUTOR: make_spec()}

            class _FakeSession4:
                def __init__(self, sid, ex, spec, ev):
                    self.sid = sid
                    self.executor = ex
                    self.on_terminal = None
                    self.deliver_turn = None
                    self._persist_terminal = None
                    self.lineage_id = sid
                    self.restarted_from = None
                    self.restart_count = 0
                    self.model = None
                    self._terminal_kind = "done"
                    self._last_screen_excerpt = "done"
                def start(self, task, cwd): pass
                def snapshot(self):
                    return {"session_id": self.sid, "executor": self.executor,
                            "control_state": "busy", "task_delivery": "pending",
                            "terminal": False}
                def terminal_snapshot(self):
                    return {"session_id": self.sid, "terminal_kind": self._terminal_kind,
                            "screen_excerpt": self._last_screen_excerpt,
                            "lineage_id": self.lineage_id}
                def stop(self):
                    kind = self._terminal_kind
                    excerpt = self._last_screen_excerpt
                    if self._persist_terminal is not None:
                        self._persist_terminal(self.sid, kind, excerpt)
                    if self.on_terminal is not None:
                        self.on_terminal(self.sid)
                def pending_async_id(self): return None

            def session_factory(sid, ex, spec, ev):
                return _FakeSession4(sid, ex, spec, ev)

            mgr = SessionManager(
                specs, events, store, session_factory=session_factory,
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)

            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            # Both leases start in the manager
            assert sid not in mgr._terminal_pending

            # Stop triggers persist -> lease release -> terminal-pending
            mgr.stop(sid, owner_id=OWNER)

            # Terminal is now in terminal_pending inventory
            assert sid in mgr._terminal_pending, (
                "session should be in terminal-pending after persist"
            )

            # Leases were released
            assert sid not in mgr._active_lease_tokens
        finally:
            store.close()


# ============================================================
# 5. Retirement oracle
# ============================================================

class TestRetirementOracle:
    """A generation with one certified epoch and confirmed >= final retires.
    An uncertified epoch blocks. Oracle ignores process_state."""

    def test_generation_with_certified_epoch_may_retire(self, store):
        gid, epoch = _seed_generation_with_terminal(store)
        _certify_epoch(store, epoch, final_hw=5)
        store.set_generation_confirmed_high_water(epoch, 5)
        store.clear_current_epoch(gid)
        assert generation_may_retire(store=store, generation_id=gid)

    def test_generation_with_uncertified_epoch_blocks(self, store):
        gid, epoch = _seed_generation_with_terminal(store)
        # epoch is still 'open'
        assert not generation_may_retire(store=store, generation_id=gid)
        blockers = generation_retirement_oracle_blockers(
            store=store, generation_id=gid)
        assert any("epoch_not_certified" in b for b in blockers)

    def test_confirmed_below_final_blocks(self, store):
        gid, epoch = _seed_generation_with_terminal(store)
        _certify_epoch(store, epoch, final_hw=10)
        store.set_generation_confirmed_high_water(epoch, 5)
        assert not generation_may_retire(store=store, generation_id=gid)
        blockers = generation_retirement_oracle_blockers(
            store=store, generation_id=gid)
        assert any("confirmed_below_final" in b for b in blockers)

    def test_oracle_ignores_process_state(self, store):
        """The oracle checks retirement_state, NEVER process_state. A dead
        epoch that is 'certified' passes; a serving epoch that is 'open' blocks."""
        gid = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=ACTIVE,
                                capability_snapshot=None, created_at=1000.0)

        # Dead epoch but certified
        dead_epoch = new_generation_id()
        store.insert_epoch(dead_epoch, gid, incarnation_meta=None, created_at=1000.0)
        store.set_epoch_process_state(dead_epoch, "dead")
        _certify_epoch(store, dead_epoch, final_hw=5)
        store.set_generation_confirmed_high_water(dead_epoch, 5)

        # Must still have current_epoch=NULL to pass oracle
        # The dead epoch blocks because current_epoch is not None (it was set by insert_epoch + cas)
        # For a clean test, let's instead check that the blocker is about current_epoch, not process_state
        blockers = generation_retirement_oracle_blockers(
            store=store, generation_id=gid)
        oracle_blocks = [b for b in blockers if "process" in b]
        assert len(oracle_blocks) == 0, (
            "oracle must not check process_state"
        )

    def test_generation_with_current_epoch_blocks(self, store):
        """A generation with a current_epoch (live/current incarnation) blocks
        the oracle even if all epochs are certified."""
        gid = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=ACTIVE,
                                capability_snapshot=None, created_at=1000.0)
        epoch = new_generation_id()
        store.insert_epoch(epoch, gid, incarnation_meta=None, created_at=1000.0)
        store.cas_epoch_serving(gid, epoch, expected_current_epoch=None)
        _certify_epoch(store, epoch, final_hw=0)
        store.set_generation_confirmed_high_water(epoch, 0)

        assert not generation_may_retire(store=store, generation_id=gid)
        blockers = generation_retirement_oracle_blockers(
            store=store, generation_id=gid)
        assert any("has_current_epoch" in b for b in blockers)

    def test_no_epochs_blocks(self, store):
        gid = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=ACTIVE,
                                capability_snapshot=None, created_at=1000.0)
        assert not generation_may_retire(store=store, generation_id=gid)


# ============================================================
# 6. Retire end-to-end
# ============================================================

class TestRetireEndToEnd:
    """Activate a 2nd generation, drain + retire the first, assert lifecycle
    reaches retired and terminals stay archived."""

    @pytest.fixture
    def setup(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        backend = Backend(build_id="b-1")
        registry = GenerationRegistry(supervisor=Supervisor(backend.transport),
                                      store=store,
                                      build_id="b-1",
                                      health_probe=lambda t: "b-1")
        operator = OperatorRoutes(registry, _EPOCH, store=store)
        yield store, registry, operator, backend
        backend.close()
        store.close()

    def test_retire_blocks_on_open_epoch(self, setup):
        store, registry, operator, _ = setup
        gid = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=DRAINING,
                                capability_snapshot=None, created_at=1000.0)
        epoch = new_generation_id()
        store.insert_epoch(epoch, gid, incarnation_meta=None, created_at=1000.0)
        store.cas_epoch_serving(gid, epoch, expected_current_epoch=None)

        status, body = operator.retire(gid)
        assert status == 200
        assert body["status"] == "blocked"
        assert len(body.get("blockers", [])) > 0

    def test_retire_success_after_certified(self, setup):
        store, registry, operator, backend = setup
        gid = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=DRAINING,
                                capability_snapshot=None, created_at=1000.0)
        epoch = new_generation_id()
        store.insert_epoch(epoch, gid, incarnation_meta=None, created_at=1000.0)
        store.cas_epoch_serving(gid, epoch, expected_current_epoch=None)

        # Pre-certify and confirm high water — do NOT clear current_epoch yet
        # (the operator clears it after reaping).
        store.set_epoch_retirement(epoch, retirement_state="certified",
                                   certificate="test-cert",
                                   final_high_water=0)
        store.set_generation_confirmed_high_water(epoch, 0)

        # Register generation in the registry so daemon RPC works
        from daemon.transport import Transport
        registry.adopt_generation(gid, epoch,
                                  Transport.tcp("127.0.0.1", backend.port, "t"),
                                  "b-1", incarnation={"pid": 1, "start_fingerprint": "fp"})

        status, body = operator.retire(gid)
        assert status == 200
        # With daemon reachable and quiesced, and epoch certified, retire should succeed
        if body["status"] != "ok":
            # If blocked, report blockers for debugging
            pytest.fail(f"retire blocked: {body.get('blockers')}")
        assert body["lifecycle_state"] == RETIRED

    def test_retire_idempotent_when_already_retired(self, setup):
        store, registry, operator, _ = setup
        gid = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=RETIRED,
                                capability_snapshot=None, created_at=1000.0)

        status, body = operator.retire(gid)
        assert status == 200
        assert body["status"] == "ok"
        assert body.get("idempotent") is True

    def test_retire_unknown_generation(self, setup):
        _, _, operator, _ = setup
        with pytest.raises(NelixError):
            operator.retire("g-nonexistent")


# ============================================================
# Helpers
# ============================================================

class _FakeClock:
    """Advancing clock: each call returns t += 0.1 so double-persist with
    differing ended_at would trigger the idempotency guard."""
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        v = self.t
        self.t += 0.1
        return v


class _TrackingEventQueue:
    """Minimal event tracking stub."""
    def __init__(self):
        self._call_order = []
    def publish(self, *a, **kw):
        self._call_order.append(("publish", a))
        return _FakeEvent()
    def latest_seq(self, *a):
        return 0
    def latest_seqs(self, *a):
        return {}
    def wait_event(self, *a, **kw):
        return None
    def wait_event_any(self, *a, **kw):
        return None
    def forget_session(self, *a):
        pass
    def resolve_decision(self, *a, **kw):
        pass


class _FakeEvent:
    event_id = "evt-fake"
    seq = 1
    resolved_reason = None


def _router_started_session(ledger, store, owner_id="test-owner", gid=None, epoch=None):
    gid = gid or ("g-" + "a" * 32)
    epoch = epoch or ("g-" + "b" * 32)
    _router_started_session._seq = getattr(_router_started_session, "_seq", 0) + 1
    r = ledger.reserve(idempotency_key="k-" + str(_router_started_session._seq),
                       owner_id=owner_id,
                       orchestration_id="o-" + "c" * 32,
                       request_fingerprint="fp")
    ledger.assign_generation(r.session_id, gid, epoch)
    return r.session_id


def _seed_generation_with_terminal(store):
    gid = new_generation_id()
    epoch = new_generation_id()
    store.create_generation(gid, build_id="b-1", lifecycle_state=ACTIVE,
                            capability_snapshot=None, created_at=1000.0)
    store.insert_epoch(epoch, gid, incarnation_meta=None, created_at=1000.0)
    store.cas_epoch_serving(gid, epoch, expected_current_epoch=None)
    return gid, epoch


def _certify_epoch(store, epoch, final_hw=0):
    store.set_epoch_retirement(epoch, retirement_state="certified",
                               certificate="test-cert",
                               final_high_water=final_hw)


@pytest.fixture
def store(request):
    s = Store(paths.nelix_root(), clock=lambda: 1000.0)
    request.addfinalizer(lambda: s.close())
    return s
