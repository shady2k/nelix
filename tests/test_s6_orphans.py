"""S6 — orphans (§3.6): observer heartbeat + /wait=observed + long grace ->
mark orphaned -> reap (orphan_reaped). Owner identity never expires; session
OBSERVATION lapses.

Tests are REAL + order-sensitive. All existing tests must stay green.
"""
import paths
from nelix_store.store import Store
from nelix_store.ledger import StartLedger

from tests.conftest import EXECUTOR, OWNER, make_spec


_EPOCH = "g-" + "0" * 32


class _FakeClock:
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        v = self.t
        self.t += 0.1
        return v
    def now(self):
        return self.__call__()


class _FakeEvent:
    event_id = "evt-fake"
    seq = 1
    resolved_reason = None


class _TrackingEventQueue:
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


def _make_sid(ledger, store, owner_id="test-owner"):
    """Reserve a session id through the ledger, return it."""
    import uuid
    key = f"k-{uuid.uuid4().hex[:8]}"
    r = ledger.reserve(idempotency_key=key, owner_id=owner_id,
                       orchestration_id="o-" + "c" * 32,
                       request_fingerprint="fp")
    ledger.assign_generation(r.session_id, "g-" + "a" * 32, _EPOCH)
    return r.session_id


class _ObservingFakeSession:
    """Fake session that exposes observation tracking for orphan tests.
    Never spawns a real process — the test drives orphan detection directly.
    Uses an externally-provided clock so observation ages match the manager's clock."""
    def __init__(self, sid, ex, spec, ev, clock=None):
        self._id = sid
        self._spec = spec
        self.executor = ex
        self._events_queue = ev
        self.on_terminal = None
        self.deliver_turn = None
        self._persist_terminal = None
        self.lineage_id = sid
        self.restarted_from = None
        self.restart_count = 0
        self.model = None
        self._last_screen_excerpt = "all done"
        # S6 observation tracking — use the passed clock (or default)
        self._clock = clock if clock is not None else _FakeClock(1000.0)
        self._lock = __import__('threading').Lock()
        self._last_observed = self._clock.now()
        self._orphan_marked_ts = None
        self._state = "awaiting_user"
        self._terminal_kind = None
        self._closing = False
        self._finalized = False
        self._stop = __import__('threading').Event()
        self._launcher = _FakeLauncher()
        self._handle = _FakeHandle()
        self._thread = None

    def start(self, task, cwd):
        pass
    def stop(self):
        self._stop.set()
        if self._launcher is not None:
            try:
                self._launcher.stop()
            except Exception:
                pass
    def snapshot(self):
        return {"session_id": self._id, "executor": self.executor,
                "control_state": self._state, "task_delivery": "delivered",
                "terminal": self._terminal_kind is not None,
                "screen_excerpt": self._last_screen_excerpt,
                "text": self._last_screen_excerpt}
    def terminal_snapshot(self):
        return {"session_id": self._id,
                "terminal_kind": self._terminal_kind or "orphan_reaped",
                "screen_excerpt": self._last_screen_excerpt,
                "lineage_id": self.lineage_id}
    def pending_async_id(self):
        return None
    def observe(self):
        with self._lock:
            self._last_observed = self._clock.now()
            self._orphan_marked_ts = None
    def mark_orphaned(self, grace):
        if grace <= 0:
            return
        with self._lock:
            now = self._clock.now()
            if now - self._last_observed < grace:
                return
            if self._orphan_marked_ts is None:
                self._orphan_marked_ts = now
    def last_observed(self):
        with self._lock:
            return self._last_observed
    def orphan_marked_ts(self):
        with self._lock:
            return self._orphan_marked_ts


class _FakeLauncher:
    def stop(self):
        pass


class _FakeHandle:
    def close(self):
        pass
    def leader_pid(self):
        return None
    def leader_pgid(self):
        return None
    def leader_status(self):
        return None


def _session_factory(clock):
    """Return a session factory that creates _ObservingFakeSession instances
    sharing the given clock."""
    def factory(sid, ex, spec, ev):
        return _ObservingFakeSession(sid, ex, spec, ev, clock=clock)
    return factory


# ============================================================
# 1. Live waiter never reaped
# ============================================================

class TestLiveWaiterNeverReaped:
    """A waiting_for_user session with an outstanding /wait is NEVER orphaned,
    even well past the observation grace period."""

    def test_live_waiter_prevents_orphaning(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            specs = {EXECUTOR: make_spec(observation_grace_seconds=50.0)}

            mgr = SessionManager(
                specs, events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)

            mgr.start(EXECUTOR, "test task", "/tmp", owner_id=OWNER,
                      session_id=sid)

            # Advance clock well past the grace (no observation yet).
            clock.t = 2000.0  # 1000s past grace

            # Register an active waiter — this should prevent orphaning.
            mgr.register_waiter(sid)
            mgr._check_orphans()

            # Session should NOT be orphan-marked.
            sess = mgr.get(sid)
            assert sess is not None, "session must still be alive (waiter active)"
            assert sess.orphan_marked_ts() is None, (
                "session must not be orphan-marked while waiter is active")

            # Even after more time passes, waiter protects it.
            clock.t = 5000.0
            mgr._check_orphans()
            assert sess.orphan_marked_ts() is None, (
                "session must still not be marked with active waiter")

            mgr.unregister_waiter(sid)
        finally:
            store.close()

    def test_waiter_unregistered_then_orphaned(self, tmp_path):
        """When the waiter finishes and no heartbeat arrives, the session
        becomes orphaned normally."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            specs = {EXECUTOR: make_spec(observation_grace_seconds=50.0)}

            mgr = SessionManager(
                specs, events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)

            mgr.start(EXECUTOR, "test task", "/tmp", owner_id=OWNER,
                      session_id=sid)
            sess = mgr.get(sid)

            # Waiter is active, protects session.
            mgr.register_waiter(sid)
            clock.t = 2000.0
            mgr._check_orphans()
            assert sess.orphan_marked_ts() is None

            # Waiter finishes — session becomes vulnerable.
            mgr.unregister_waiter(sid)

            # Advance well past grace with no waiter.
            clock.t = 3000.0
            mgr._check_orphans()
            assert sess.orphan_marked_ts() is not None, (
                "session must be orphan-marked after waiter leaves and grace elapses")
        finally:
            store.close()


# ============================================================
# 2. Grace -> mark -> reap
# ============================================================

class TestGraceMarkReap:
    """Full lifecycle: unobserved past grace -> marked orphaned -> reaped."""

    def test_grace_then_mark(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            specs = {EXECUTOR: make_spec(observation_grace_seconds=50.0)}

            mgr = SessionManager(
                specs, events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)

            mgr.start(EXECUTOR, "test task", "/tmp", owner_id=OWNER,
                      session_id=sid)
            sess = mgr.get(sid)

            assert sess.orphan_marked_ts() is None, "fresh session not marked"

            # Advance past grace, no observation.
            clock.t = 1100.0  # +100s, past 50s grace
            mgr._check_orphans()

            assert sess.orphan_marked_ts() is not None, "session marked orphaned"
            assert mgr.get(sid) is not None, "session still alive (only marked)"
        finally:
            store.close()

    def test_mark_then_reap(self, tmp_path):
        """Marked session that stays unobserved past grace is reaped:
        orphan_reaped terminal persisted, session gone, leases released,
        obligation discharged, owner namespace untouched."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            specs = {EXECUTOR: make_spec(observation_grace_seconds=50.0)}

            mgr = SessionManager(
                specs, events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)

            mgr.start(EXECUTOR, "test task", "/tmp", owner_id=OWNER,
                      session_id=sid)

            # Mark: advance past grace.
            clock.t = 1100.0
            mgr._check_orphans()
            sess = mgr.get(sid)
            assert sess is not None
            assert sess.orphan_marked_ts() is not None

            # Reap: advance well past another grace period.
            clock.t = 1200.0  # +200s since start, 2x grace
            mgr._check_orphans()
            mgr._reap_orphan(sid)

            # Session gone from manager.
            assert mgr.get(sid) is None, "session removed after reap"

            # Terminal record in store (orphan_reaped).
            terminal = store.get_terminal(sid, owner_id=OWNER)
            assert terminal is not None, "terminal record persisted"
            assert terminal.terminal_kind == "orphan_reaped", (
                f"terminal kind must be orphan_reaped, got {terminal.terminal_kind}")

            # Owner namespace is untouched (owner record still on disk).
            from daemon import owner
            assert owner.owns_session(sid, OWNER) is not None, (
                "owner namespace must survive the session")

            # Leases released (no active/live tokens).
            with mgr._lock:
                assert sid not in mgr._active_lease_tokens
                assert sid not in mgr._live_lease_tokens
                assert sid not in mgr._terminal_obligations

            # Events contain orphan_reaped publish.
            published_kinds = [a[1][2] for a in events._call_order
                               if a[0] == "publish"]
            assert "orphan_reaped" in published_kinds, (
                f"orphan_reaped event must be published, got {published_kinds}")
        finally:
            store.close()


# ============================================================
# 3. Recovery — marked session re-observed, un-marked
# ============================================================

class TestRecovery:
    """A MARKED (not yet reaped) session that gets re-observed is un-marked
    and survives."""

    def test_marked_recovered_by_heartbeat(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            specs = {EXECUTOR: make_spec(observation_grace_seconds=50.0)}

            mgr = SessionManager(
                specs, events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)

            mgr.start(EXECUTOR, "test task", "/tmp", owner_id=OWNER,
                      session_id=sid)
            sess = mgr.get(sid)

            # Advance past grace -> marked.
            clock.t = 1100.0
            mgr._check_orphans()
            assert sess.orphan_marked_ts() is not None

            # Heartbeat arrives (re-observation).
            mgr.observe_session(sid)
            assert sess.orphan_marked_ts() is None, "un-marked after observe"
            assert mgr.get(sid) is not None, "session survives"

            # Next check: not marked again (observation is recent).
            clock.t = 1120.0
            mgr._check_orphans()
            assert sess.orphan_marked_ts() is None, "still not marked"
        finally:
            store.close()

    def test_marked_recovered_by_waiter(self, tmp_path):
        """A new /wait on a marked session un-marks it."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            specs = {EXECUTOR: make_spec(observation_grace_seconds=50.0)}

            mgr = SessionManager(
                specs, events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)

            mgr.start(EXECUTOR, "test task", "/tmp", owner_id=OWNER,
                      session_id=sid)
            sess = mgr.get(sid)

            # Advance past grace -> marked.
            clock.t = 1100.0
            mgr._check_orphans()
            assert sess.orphan_marked_ts() is not None

            # Register waiter (re-observation via /wait).
            mgr.register_waiter(sid)
            mgr._check_orphans()
            assert sess.orphan_marked_ts() is None, "un-marked by active waiter"
        finally:
            store.close()


# ============================================================
# 4. Heartbeat renews observation
# ============================================================

class TestHeartbeatRenews:
    """A heartbeat within the grace period keeps the session alive."""

    def test_heartbeat_within_grace_prevents_orphaning(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            specs = {EXECUTOR: make_spec(observation_grace_seconds=100.0)}

            mgr = SessionManager(
                specs, events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)

            mgr.start(EXECUTOR, "test task", "/tmp", owner_id=OWNER,
                      session_id=sid)
            sess = mgr.get(sid)

            # Heartbeat at t=1050 (within 100s grace from t=1000).
            clock.t = 1050.0
            mgr.observe_session(sid)

            # Advance to t=1100 (50s since heartbeat, within grace).
            clock.t = 1100.0
            mgr._check_orphans()
            assert sess.orphan_marked_ts() is None, "not marked (within grace)"

            # Advance past grace from last heartbeat (t=1050 + 100 = t=1150).
            clock.t = 1200.0
            mgr._check_orphans()
            assert sess.orphan_marked_ts() is not None, "marked (past grace)"
        finally:
            store.close()


# ============================================================
# 5. Distinctness — orphan_reaped != max_idle_seconds
# ============================================================

class TestDistinctness:
    """orphan_reaped is distinct from max_idle_seconds escalation and
    generation_lost."""

    def test_orphan_reaped_is_not_max_idle(self, tmp_path):
        """Prove orphan detection and max_idle_seconds are independent
        config knobs with different effects."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            # Short max_idle (10s) but long observation grace (500s).
            specs = {EXECUTOR: make_spec(
                max_idle_seconds=10.0,
                observation_grace_seconds=500.0,
            )}

            mgr = SessionManager(
                specs, events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)

            mgr.start(EXECUTOR, "test task", "/tmp", owner_id=OWNER,
                      session_id=sid)

            # Advance past max_idle but well within observation grace.
            clock.t = 1050.0  # 50s past start, >10s max_idle, <500s grace
            mgr._check_orphans()
            sess = mgr.get(sid)
            assert sess.orphan_marked_ts() is None, (
                "orphan NOT marked even though max_idle elapsed "
                "(distinct mechanisms)")
        finally:
            store.close()

    def test_orphan_reaped_is_not_generation_lost(self, tmp_path):
        """Prove orphan_reaped is a terminal kind that appears on the owner's
        board, distinct from generation_lost error code."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            specs = {EXECUTOR: make_spec(observation_grace_seconds=50.0)}

            mgr = SessionManager(
                specs, events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)

            mgr.start(EXECUTOR, "test task", "/tmp", owner_id=OWNER,
                      session_id=sid)

            # Mark and reap.
            clock.t = 1100.0
            mgr._check_orphans()
            clock.t = 1200.0
            mgr._check_orphans()
            mgr._reap_orphan(sid)

            # The terminal kind is "orphan_reaped", not "generation_lost".
            terminal = store.get_terminal(sid, owner_id=OWNER)
            assert terminal.terminal_kind == "orphan_reaped"

            # The terminal appears on the owner's board (list_terminal).
            board = store.list_terminal(OWNER)
            board_sids = [t.session_id for t in board]
            assert sid in board_sids, "orphan_reaped terminal on owner board"
        finally:
            store.close()
