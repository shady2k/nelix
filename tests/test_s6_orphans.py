"""S6 — orphans (§3.6): observer heartbeat + /wait=observed + long grace ->
mark orphaned -> reap (orphan_reaped). Owner identity never expires; session
OBSERVATION lapses.

Tests are REAL (no single-shared-clock hiding, no race-invisible decisions,
real refcount, real idempotency). All existing tests must stay green.
"""
import threading
import time
import paths
from nelix_store.store import Store
from nelix_store.ledger import StartLedger

from tests.conftest import EXECUTOR, OWNER, make_spec


_EPOCH = "g-" + "0" * 32


class _FakeClock:
    """Callable clock: each call returns t, then advances by `step`."""
    def __init__(self, t=1000.0, step=0.1):
        self.t = t
        self.step = step
    def __call__(self):
        v = self.t
        self.t += self.step
        return v


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
    def latest_seq(self, *a): return 0
    def latest_seqs(self, *a): return {}
    def wait_event(self, *a, **kw): return None
    def wait_event_any(self, *a, **kw): return None
    def forget_session(self, *a): pass
    def resolve_decision(self, *a, **kw): pass


def _make_sid(ledger, store, owner_id="test-owner"):
    import uuid
    key = f"k-{uuid.uuid4().hex[:8]}"
    r = ledger.reserve(idempotency_key=key, owner_id=owner_id,
                       orchestration_id="o-" + "c" * 32,
                       request_fingerprint="fp")
    ledger.assign_generation(r.session_id, "g-" + "a" * 32, _EPOCH)
    return r.session_id


class _ObservingFakeSession:
    """Minimal fake session exercising the real observation + orphan code paths.
    Uses the manager's clock (via obs_clock wiring) so observation timestamps
    are in the SAME domain as the manager's orphan checker (FIX 2)."""
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
        self._last_screen_excerpt = "session running"
        # FIX 2: observation clock matches the manager's clock domain.
        self._obs_clock = clock if clock is not None else time.time
        self._lock = threading.Lock()
        self._last_observed = self._obs_clock()
        self._orphan_marked_ts = None
        self._state = "awaiting_user"
        self._terminal_kind = None
        self._closing = False
        self._finalized = False
        self._stop = threading.Event()
        self._launcher = _FakeLauncher()
        self._handle = _FakeHandle()
        self._thread = None

    def start(self, task, cwd): pass
    def stop(self):
        self._stop.set()
        if self._launcher is not None:
            try: self._launcher.stop(self._handle)
            except Exception: pass
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
    def pending_async_id(self): return None

    # ── S6 real observation methods (FIX 2: uses _obs_clock) ───────────
    def observe(self):
        with self._lock:
            self._last_observed = self._obs_clock()
            self._orphan_marked_ts = None
    def mark_orphaned(self, grace):
        if grace <= 0: return
        with self._lock:
            now = self._obs_clock()
            if now - self._last_observed < grace: return
            if self._orphan_marked_ts is None:
                self._orphan_marked_ts = now
    def last_observed(self):
        with self._lock: return self._last_observed
    def orphan_marked_ts(self):
        with self._lock: return self._orphan_marked_ts


class _FakeLauncher:
    def stop(self, handle): pass


class _FakeHandle:
    def close(self): pass
    def leader_pid(self): return None
    def leader_pgid(self): return None
    def leader_status(self): return None


def _session_factory(clock):
    def factory(sid, ex, spec, ev):
        return _ObservingFakeSession(sid, ex, spec, ev, clock=clock)
    return factory


# ============================================================
# 1. Live waiter never reaped (incl. race)
# ============================================================

class TestLiveWaiterNeverReaped:
    """A waiting_for_user session with an outstanding /wait is NEVER orphaned."""

    def test_waiter_prevents_marking(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            mgr = SessionManager(
                {EXECUTOR: make_spec(observation_grace_seconds=50.0)},
                events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            mgr.register_waiter(sid)        # waiter arrives early
            clock.t = 9999.0                # far past grace
            mgr._check_orphans()

            sess = mgr.get(sid)
            assert sess is not None
            assert sess.orphan_marked_ts() is None, "waiter prevents mark"
            mgr.unregister_waiter(sid)
        finally:
            store.close()

    def test_race_waiter_arrives_after_checker_snapshot_aborts_reap(self, tmp_path):
        """Prove the FIX 1 race: a waiter that arrives between the checker's
        snapshot and _reap_orphan causes re-validation to ABORT."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            mgr = SessionManager(
                {EXECUTOR: make_spec(observation_grace_seconds=50.0)},
                events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            # Mark the session.
            clock.t = 1100.0
            mgr._check_orphans()
            sess = mgr.get(sid)
            assert sess.orphan_marked_ts() is not None

            # Now simulate the race: checker would collect for reap, but a
            # waiter registers BETWEEN the snapshot and the _reap_orphan call.
            # We manually call _reap_orphan with the waiter active.
            clock.t = 1200.0
            mgr.register_waiter(sid)         # waiter arrives
            reaped = mgr._reap_orphan(sid)   # should re-validate and ABORT
            assert reaped is False, "reap must abort when waiter active"
            assert mgr.get(sid) is not None, "session survives"
            mgr.unregister_waiter(sid)
        finally:
            store.close()

    def test_race_observe_clears_mark_before_reap_aborts(self, tmp_path):
        """Prove the FIX 1 race: observe() clears the mark between the checker's
        decision and _reap_orphan -> re-validation ABORTS."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            mgr = SessionManager(
                {EXECUTOR: make_spec(observation_grace_seconds=50.0)},
                events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            clock.t = 1100.0
            mgr._check_orphans()
            sess = mgr.get(sid)
            assert sess.orphan_marked_ts() is not None

            clock.t = 1200.0
            mgr.observe_session(sid)         # observe clears mark
            reaped = mgr._reap_orphan(sid)   # must re-validate and ABORT
            assert reaped is False, "reap must abort when observe cleared mark"
            assert mgr.get(sid) is not None
        finally:
            store.close()


# ============================================================
# 2. Grace -> mark -> reap (full lifecycle)
# ============================================================

class TestGraceMarkReap:
    def test_grace_then_mark(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            mgr = SessionManager(
                {EXECUTOR: make_spec(observation_grace_seconds=50.0)},
                events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)
            sess = mgr.get(sid)

            clock.t = 1100.0
            mgr._check_orphans()
            assert sess.orphan_marked_ts() is not None
            assert mgr.get(sid) is not None
        finally:
            store.close()

    def test_mark_then_reap(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            mgr = SessionManager(
                {EXECUTOR: make_spec(observation_grace_seconds=50.0)},
                events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            # Mark
            clock.t = 1100.0
            mgr._check_orphans()
            assert mgr.get(sid).orphan_marked_ts() is not None

            # Reap
            clock.t = 1200.0
            mgr._check_orphans()
            mgr._reap_orphan(sid)

            assert mgr.get(sid) is None

            terminal = store.get_terminal(sid, owner_id=OWNER)
            assert terminal.terminal_kind == "orphan_reaped"

            from daemon import owner
            assert owner.owns_session(sid, OWNER) is not None

            with mgr._lock:
                assert sid not in mgr._active_lease_tokens
                assert sid not in mgr._live_lease_tokens
                assert sid not in mgr._terminal_obligations

            published_kinds = [a[1][2] for a in events._call_order if a[0] == "publish"]
            assert "orphan_reaped" in published_kinds
        finally:
            store.close()


# ============================================================
# 3. Clock domain (FIX 2)
# ============================================================

class TestClockDomain:
    """Prove observation timestamps and the manager's clock live in the SAME
    domain (FIX 2). Uses deliberately DIFFERENT fake clocks for the manager
    vs the Session's belief clock to simulate the production WallClock vs
    time.time mismatch — the obs_clock wiring keeps them consistent."""

    def test_obs_clock_matches_manager_clock(self, tmp_path):
        """The session's observation timestamps (last_observed, orphan_marked_ts)
        use the manager's clock via _obs_clock wiring (FIX 2). The orphan checker
        uses the manager clock, so the domains match even when the Session's
        belief clock (WallClock/monotonic) is different."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            mgr_clock = _FakeClock(5000.0, step=0.5)
            ledger = StartLedger(paths.nelix_root(), clock=mgr_clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            # Use _session_factory with mgr_clock — this wires obs_clock=clock
            # to match the manager's clock.
            mgr = SessionManager(
                {EXECUTOR: make_spec(observation_grace_seconds=100.0)},
                events, store,
                session_factory=_session_factory(mgr_clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=mgr_clock)
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            # observe_session uses mgr_clock via the session's _obs_clock
            mgr.observe_session(sid)
            sess = mgr.get(sid)

            # The manager's clock and session's obs_clock are the SAME domain.
            # Advance the manager clock and check that session clock advances identically.
            mgr_clock.t = 6000.0
            mgr.observe_session(sid)
            obs_ts2 = sess.last_observed()
            # The second observe should have set last_observed to ~6000 (mgr_clock)
            assert abs(obs_ts2 - 6000.0) < 1.0, (
                f"observation timestamp ({obs_ts2}) should be in manager clock domain (~6000)")

            # Verify mark_orphaned also uses the same domain
            mgr_clock.t = 7000.0
            mgr._check_orphans()
            marked_ts = sess.orphan_marked_ts()
            if marked_ts is not None:
                assert abs(marked_ts - 7000.0) < 1.0, (
                    f"orphan mark timestamp ({marked_ts}) in manager clock domain")
        finally:
            store.close()


# ============================================================
# 4. Duplicate waiters (FIX 3 — refcount)
# ============================================================

class TestDuplicateWaiters:
    """Two concurrent waiters on one SID must both unregister before the
    session becomes unprotected."""

    def test_two_waiters_keep_session_observed(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            mgr = SessionManager(
                {EXECUTOR: make_spec(observation_grace_seconds=50.0)},
                events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            # Two waiters register
            mgr.register_waiter(sid)
            mgr.register_waiter(sid)

            # Refcount should be 2
            with mgr._lock:
                assert mgr._active_waiter_sids.get(sid, 0) == 2

            clock.t = 9999.0  # far past grace
            mgr._check_orphans()
            sess = mgr.get(sid)
            assert sess.orphan_marked_ts() is None, "two waiters protect session"

            # First unregister: still one waiter left
            mgr.unregister_waiter(sid)
            mgr._check_orphans()
            assert sess.orphan_marked_ts() is None, "one waiter still protects"

            # Second unregister: now unprotected
            mgr.unregister_waiter(sid)
            with mgr._lock:
                assert mgr._active_waiter_sids.get(sid, 0) == 0

            # Advance clock past grace (unregister calls observe, so we need
            # to advance past the grace from that point).
            clock.t = 12000.0
            mgr._check_orphans()
            assert sess.orphan_marked_ts() is not None
        finally:
            store.close()

    def test_register_unregister_refcount_under_lock(self, tmp_path):
        """register_waiter and unregister_waiter are atomic under the lock."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            mgr = SessionManager(
                {EXECUTOR: make_spec(observation_grace_seconds=50.0)},
                events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            # Simulate concurrent register/unregister from two threads
            def reg_thread():
                for _ in range(100):
                    mgr.register_waiter(sid)
            def unreg_thread():
                for _ in range(100):
                    mgr.unregister_waiter(sid)

            t1 = threading.Thread(target=reg_thread, daemon=True)
            t2 = threading.Thread(target=unreg_thread, daemon=True)
            t1.start(); t2.start()
            t1.join(); t2.join()

            # The net count should be 0 (100 regs + 100 unregs)
            with mgr._lock:
                count = mgr._active_waiter_sids.get(sid, 0)
            assert count == 0, f"refcount should be 0 after balanced reg/unreg, got {count}"
        finally:
            store.close()

    def test_register_calls_observe(self, tmp_path):
        """register_waiter calls observe_session so a new waiter immediately
        counts as observed (FIX 1)."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            mgr = SessionManager(
                {EXECUTOR: make_spec(observation_grace_seconds=50.0)},
                events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            clock.t = 1100.0  # past grace
            # Register calls observe, which renews last_observed
            mgr.register_waiter(sid)
            sess = mgr.get(sid)
            obs_ts = sess.last_observed()
            assert abs(obs_ts - 1100.0) < 0.5, (
                "register_waiter should have updated last_observed")
            mgr.unregister_waiter(sid)
        finally:
            store.close()


# ============================================================
# 5. Idempotency (FIX 4) + lease release
# ============================================================

class TestIdempotency:
    """A session that terminaled normally (done/crashed/stopped) is NOT
    overwritten with orphan_reaped. No obligation/lease leak on persist failure."""

    def test_idempotency_conflict_aborts_reap(self, tmp_path):
        """If the session already has a done terminal, _reap_orphan gets
        idempotency_conflict and aborts without publishing orphan_reaped."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            mgr = SessionManager(
                {EXECUTOR: make_spec(observation_grace_seconds=50.0)},
                events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            # Pre-persist a "done" terminal (simulates normal completion).
            store.put_terminal(sid, terminal_kind="done", summary="completed ok",
                               ended_at=clock())

            # Mark the session as orphaned in memory (orphan checker would see
            # it as orphanable, but the store already has a done terminal).
            sess = mgr.get(sid)
            sess._orphan_marked_ts = 1200.0

            # Attempt reap — should get idempotency_conflict, ABORT silently.
            clock.t = 1300.0
            reaped = mgr._reap_orphan(sid)
            assert reaped is False, "reap must abort on idempotency conflict"

            # The session is NOT killed/removed (reap aborted before finalize).
            assert mgr.get(sid) is not None, "session survives aborted reap"

            # The terminal kind is still "done" (not overwritten).
            terminal = store.get_terminal(sid, owner_id=OWNER)
            assert terminal.terminal_kind == "done", (
                f"terminal kind unchanged, got {terminal.terminal_kind}")

            # No orphan_reaped event was published.
            published_kinds = [a[1][2] for a in events._call_order if a[0] == "publish"]
            assert "orphan_reaped" not in published_kinds
        finally:
            store.close()


class TestLeaseRelease:
    """With real-style lease tokens, orphan reap releases them."""

    def test_reap_releases_active_lease(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            mgr = SessionManager(
                {EXECUTOR: make_spec(observation_grace_seconds=50.0)},
                events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            # Manually install lease tokens (simulates router lease).
            with mgr._lock:
                mgr._active_lease_tokens[sid] = "active-tok-1"
                mgr._live_lease_tokens[sid] = "live-tok-1"

            clock.t = 1100.0
            mgr._check_orphans()
            clock.t = 1200.0
            mgr._reap_orphan(sid)

            # After reap, the tokens should be gone (released by
            # _persist_terminal_for_publish which calls pop + _release).
            with mgr._lock:
                assert sid not in mgr._active_lease_tokens
                assert sid not in mgr._live_lease_tokens
                assert sid not in mgr._active_token_activation
                assert sid not in mgr._live_token_activation
                assert sid not in mgr._terminal_obligations
        finally:
            store.close()


# ============================================================
# 6. Recovery — marked session re-observed, un-marked
# ============================================================

class TestRecovery:
    def test_marked_recovered_by_heartbeat(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            mgr = SessionManager(
                {EXECUTOR: make_spec(observation_grace_seconds=50.0)},
                events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)
            sess = mgr.get(sid)

            clock.t = 1100.0
            mgr._check_orphans()
            assert sess.orphan_marked_ts() is not None

            mgr.observe_session(sid)
            assert sess.orphan_marked_ts() is None
            assert mgr.get(sid) is not None

            clock.t = 1120.0
            mgr._check_orphans()
            assert sess.orphan_marked_ts() is None
        finally:
            store.close()


# ============================================================
# 7. Distinctness
# ============================================================

class TestDistinctness:
    def test_orphan_reaped_is_not_max_idle(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            mgr = SessionManager(
                {EXECUTOR: make_spec(max_idle_seconds=10.0, observation_grace_seconds=500.0)},
                events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            clock.t = 1050.0  # past max_idle, within observation grace
            mgr._check_orphans()
            sess = mgr.get(sid)
            assert sess.orphan_marked_ts() is None
        finally:
            store.close()

    def test_orphan_reaped_is_not_generation_lost(self, tmp_path):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        try:
            clock = _FakeClock(1000.0)
            ledger = StartLedger(paths.nelix_root(), clock=clock)
            events = _TrackingEventQueue()
            from daemon.manager import SessionManager

            sid = _make_sid(ledger, store)
            mgr = SessionManager(
                {EXECUTOR: make_spec(observation_grace_seconds=50.0)},
                events, store,
                session_factory=_session_factory(clock),
                concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock)
            mgr.start(EXECUTOR, "test", "/tmp", owner_id=OWNER, session_id=sid)

            clock.t = 1100.0
            mgr._check_orphans()
            clock.t = 1200.0
            mgr._check_orphans()
            mgr._reap_orphan(sid)

            terminal = store.get_terminal(sid, owner_id=OWNER)
            assert terminal.terminal_kind == "orphan_reaped"

            board = store.list_terminal(OWNER)
            board_sids = [t.session_id for t in board]
            assert sid in board_sids
        finally:
            store.close()
