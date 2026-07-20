"""Tests for the router lease service and lease-based admission.
"""
import threading
import uuid

import pytest
from tests.conftest import EXECUTOR, OWNER, make_spec, reserve_start

from daemon.events import EventQueue
from daemon.lease_client import LeaseClient
from daemon.manager import SessionManager, RespondOutcome
from router.leases import LeaseService
from nelix_contracts.errors import (
    ADMISSION_UNAVAILABLE, STALE_RECONCILIATION_ID, NelixError,
)


class FakeSession:
    def __init__(self, sid, executor, *a, **k):
        self.sid = sid; self.executor = executor; self.started = None
        self.started_cwd = None; self.stopped = False; self.task = None; self.cwd = None
        self.on_idle = None
        self._activation_counter = 0
        self._active_lease_token = None
        self._live_lease_token = None
    def start(self, task, cwd): self.started = task; self.started_cwd = cwd; self.task = task; self.cwd = cwd
    def respond(self, answer, decision_id=None):
        return RespondOutcome("resumed", seq=1, decision_id="dec-1")
    def snapshot(self):
        cs = getattr(self, "_control_state", "busy")
        return {"session_id": self.sid, "executor": self.executor,
                "control_state": cs, "task_delivery": "delivered"}
    def stop(self): self.stopped = True
    def send_turn(self, text):
        return RespondOutcome("resumed", seq=2)


class _MockLeaseClient:
    """Mock lease client backed by a real LeaseService for testing."""

    def __init__(self, active_limit=5, live_pty_limit=5):
        self._service = LeaseService(active_limit=active_limit,
                                      live_pty_limit=live_pty_limit)
        self._gen_id = "g-" + uuid.uuid4().hex
        self._gen_epoch = "g-" + uuid.uuid4().hex

    @property
    def service(self):
        return self._service

    def acquire(self, generation_id, generation_epoch, session_id,
                activation_id, kinds):
        key = (str(generation_id), str(generation_epoch),
               str(session_id), str(activation_id))
        return self._service.acquire(key, kinds)

    def release(self, token_id):
        return self._service.release(token_id)

    def needs_handshake(self):
        return False

    def mark_handshook(self, rid):
        pass

    def retry_outbox(self):
        return []

    def register_snapshot(self, *a, **k):
        raise self.RouterUnavailable("mock no route")

    @property
    def reconciliation_id(self):
        return getattr(self, "_mock_rid", None)

    @reconciliation_id.setter
    def reconciliation_id(self, value):
        self._mock_rid = value


def _tid(info):
    """Extract token_id string from acquire result entry (dict or legacy)."""
    return info["token_id"] if isinstance(info, dict) else info


# ── LeaseService unit tests ──────────────────────────────────────────────


class TestLeaseService:
    def test_acquire_active_caps_cross_generations(self):
        """Active bound is ONE counter across different generation_ids."""
        svc = LeaseService(active_limit=2)
        svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        svc.acquire(("g-2", "e-2", "s-2", "0"), {"active"})
        assert svc.active_count == 2
        with pytest.raises(NelixError):
            svc.acquire(("g-3", "e-3", "s-3", "0"), {"active"})

    def test_active_bound(self):
        svc = LeaseService(active_limit=1)
        svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        assert svc.active_count == 1
        with pytest.raises(NelixError):
            svc.acquire(("g-2", "e-2", "s-2", "0"), {"active"})

    def test_live_pty_bound_independent(self):
        svc = LeaseService(active_limit=2, live_pty_limit=1)
        t1 = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        t2 = svc.acquire(("g-2", "e-2", "s-2", "0"), {"active", "live"})
        assert svc.active_count == 2
        assert svc.live_pty_count == 1
        with pytest.raises(NelixError):
            svc.acquire(("g-3", "e-3", "s-3", "0"), {"active"})
        svc.release(_tid(t1["active"]))
        assert svc.active_count == 1
        assert svc.live_pty_count == 1
        svc.release(_tid(t2["live"]))
        assert svc.live_pty_count == 0

    # FIX A1: all-or-nothing atomic acquire
    def test_acquire_atomic_no_leak_on_live_cap(self):
        """Acquiring {active, live} when live at cap does NOT mutate anything (FIX A1)."""
        svc = LeaseService(active_limit=2, live_pty_limit=1)
        # Fill live
        svc.acquire(("g-1", "e-1", "s-1", "0"), {"live"})
        assert svc.active_count == 0
        assert svc.live_pty_count == 1
        # Try to acquire both — should fail WITHOUT touching active_count.
        with pytest.raises(NelixError, match="live"):
            svc.acquire(("g-2", "e-2", "s-2", "0"), {"active", "live"})
        # active_count MUST be unchanged (FIX A1: no orphaned lease).
        assert svc.active_count == 0, "active_count leaked despite live cap"
        assert svc.live_pty_count == 1

    # FIX A2: idempotent-by-key counted once
    def test_idempotent_acquire_same_key(self):
        """Same key returns same token, counter unchanged (FIX A2)."""
        svc = LeaseService(active_limit=1)
        r1 = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        assert svc.active_count == 1
        assert r1["active"]["fresh"] is True
        r2 = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        assert r2["active"]["token_id"] == r1["active"]["token_id"]
        assert r2["active"]["fresh"] is False
        assert svc.active_count == 1  # not double-counted

    def test_release_exactly_once_no_undercount(self):
        """Release frees the slot exactly once; stale release is no-op (FIX A2)."""
        svc = LeaseService(active_limit=1)
        r = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        tid = _tid(r["active"])
        assert svc.active_count == 1
        # First release frees the slot.
        assert svc.release(tid) is True
        assert svc.active_count == 0
        # Second (stale) release is a safe no-op.
        assert svc.release(tid) is False
        assert svc.active_count == 0

    def test_lost_acquire_response_retry_no_leak(self):
        """Retry after lost response returns same token; one release frees (FIX A2)."""
        svc = LeaseService(active_limit=1)
        # First acquire succeeds on router (simulated).
        r1 = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        tid = _tid(r1["active"])
        assert svc.active_count == 1
        # Response lost; daemon retries with same key.
        r2 = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        assert r2["active"]["fresh"] is False  # not freshly counted
        assert svc.active_count == 1  # unchanged
        # Daemon holds ONE token (from the retry response), releases once.
        assert svc.release(tid) is True
        assert svc.active_count == 0  # back to baseline — no leak

    def test_release_unknown_token(self):
        svc = LeaseService()
        assert svc.release("nonexistent") is False

    def test_release_after_acquire_free(self):
        svc = LeaseService(active_limit=1)
        r = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        assert svc.release(_tid(r["active"])) is True
        assert svc.active_count == 0

    def test_rebuilding_raises(self):
        svc = LeaseService()
        # Register the epoch first, then set rebuilding to clear reconciled_rid.
        svc.register_snapshot("g-1", "e-1", svc.reconciliation_id,
                               cutoff_revision=0, active_tokens=[], live_tokens=[])
        svc.set_rebuilding(True)
        with pytest.raises(NelixError):
            svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                        reconciliation_id=svc.reconciliation_id,
                        transition_revision=1)


# ── MockLeaseClient tests ────────────────────────────────────────────────

def test_mock_client_passthrough():
    client = _MockLeaseClient(active_limit=1)
    r = client.acquire("g-1", "e-1", "s-1", 0, {"active"})
    assert "active" in r
    assert client.service.active_count == 1
    client.release(_tid(r["active"]))
    assert client.service.active_count == 0


# ── Manager integration tests (lease-based admission) ────────────────────


class _LeasedFakeSession(FakeSession):
    """Fake session that supports the lease contract (on_idle, _activation_counter)."""
    def __init__(self, sid, executor, *a, **k):
        super().__init__(sid, executor, *a, **k)
        self._control_state = "busy"
        self._activation_counter = 0
        self.on_idle = None
        self.on_terminal = None

    def snapshot(self):
        return {"session_id": self.sid, "executor": self.executor,
                "control_state": self._control_state,
                "task_delivery": "delivered"}

    def send_turn(self, text):
        self._control_state = "busy"
        return RespondOutcome("resumed", seq=2)

    def stop(self):
        self.stopped = True
        if self.on_terminal is not None:
            self.on_terminal(self.sid)


def _lease_mgr(store_and_ledger, active_limit=2, live_pty_limit=2):
    store, ledger = store_and_ledger
    specs = {EXECUTOR: make_spec()}
    q = EventQueue()
    lease_client = _MockLeaseClient(active_limit=active_limit,
                                     live_pty_limit=live_pty_limit)
    made = []
    def session_factory(sid, executor, spec, events):
        s = _LeasedFakeSession(sid, executor)
        made.append(s)
        return s
    m = SessionManager(
        specs, q, store, session_factory=session_factory,
        concurrency_limit=active_limit,
        lease_client=lease_client,
        generation_id="g-leased",
        generation_epoch="e-leased",
    )
    return m, made, ledger, lease_client


class TestLeaseAdmission:
    def test_start_acquires_active_and_live(self, store_and_ledger):
        mgr, _, ledger, client = _lease_mgr(store_and_ledger, active_limit=5)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        assert client.service.active_count == 1
        assert client.service.live_pty_count == 1

    def test_active_bound_caps_across_sessions(self, store_and_ledger):
        mgr, _, ledger, client = _lease_mgr(store_and_ledger, active_limit=1)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        sid2 = reserve_start(ledger)
        with pytest.raises(NelixError, match="active.*limit"):
            mgr.start(EXECUTOR, "task B", "/tmp", owner_id=OWNER, session_id=sid2)

    def test_terminal_releases_both_leases(self, store_and_ledger):
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=1)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        assert client.service.active_count == 1
        sess = made[0]
        sess._control_state = "terminal"
        sess.on_terminal(sid)
        assert client.service.active_count == 0
        assert client.service.live_pty_count == 0
        sid2 = reserve_start(ledger)
        mgr.start(EXECUTOR, "task B", "/tmp", owner_id=OWNER, session_id=sid2)
        assert client.service.active_count == 1

    def test_racing_send_turns_same_idle_session(self, store_and_ledger):
        """Two racing send_turns for the same idle session don't double-consume."""
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=2)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        sess = made[0]
        sess._control_state = "idle"
        results = []
        def send():
            r = mgr.send_turn(sid, "hello")
            results.append(r)
        t1 = threading.Thread(target=send)
        t2 = threading.Thread(target=send)
        t1.start(); t2.start()
        t1.join(); t2.join()
        successes = [r for r in results if r.status == "resumed"]
        assert len(successes) <= 1, \
            f"Expected at most 1 success, got {len(successes)}: {[r.status for r in results]}"
        assert client.service.active_count <= 2

    def test_send_turn_releases_active_on_idle(self, store_and_ledger):
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=2)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        sess = made[0]
        assert client.service.active_count == 1
        if sess.on_idle is not None:
            sess.on_idle(sid)
        assert client.service.active_count == 0
        assert client.service.live_pty_count == 1

    def test_send_turn_acquires_active_only(self, store_and_ledger):
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=2)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        sess = made[0]
        if sess.on_idle is not None:
            sess.on_idle(sid)
        assert client.service.active_count == 0
        assert client.service.live_pty_count == 1
        sess._control_state = "idle"
        mgr.send_turn(sid, "follow-up")
        assert client.service.active_count == 1
        assert client.service.live_pty_count == 1

    def test_router_unavailable_start(self, store_and_ledger):
        """FIX C1: start with unreachable router raises NelixError(admission_unavailable)."""
        bad_client = LeaseClient("/nonexistent/router.sock", timeout=0.1)
        store, ledger = store_and_ledger
        specs = {EXECUTOR: make_spec()}
        q = EventQueue()
        mgr = SessionManager(specs, q, store,
                             session_factory=lambda sid, ex, sp, ev: _LeasedFakeSession(sid, ex),
                             concurrency_limit=5, lease_client=bad_client,
                             generation_id="g-1", generation_epoch="e-1")
        sid = reserve_start(ledger)
        with pytest.raises(NelixError) as exc_info:
            mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        assert exc_info.value.code == ADMISSION_UNAVAILABLE
        assert exc_info.value.retryable is True

    def test_router_unavailable_send_turn(self, store_and_ledger):
        """FIX C2: send_turn with unreachable router returns admission_unavailable."""
        bad_client = LeaseClient("/nonexistent/router.sock", timeout=0.1)
        store, ledger = store_and_ledger
        specs = {EXECUTOR: make_spec()}
        q = EventQueue()
        mgr = SessionManager(specs, q, store,
                             session_factory=lambda sid, ex, sp, ev: _LeasedFakeSession(sid, ex),
                             concurrency_limit=5, lease_client=bad_client,
                             generation_id="g-1", generation_epoch="e-1")
        from tests.conftest import own
        sid = "s-" + "a" * 32
        sess = _LeasedFakeSession(sid, EXECUTOR)
        sess._control_state = "idle"
        own(sid)
        with mgr._lock:
            mgr._sessions[sid] = sess
        outcome = mgr.send_turn(sid, "hello")
        assert outcome.status == "admission_unavailable"

    # FIX B1: restart always acquires router leases
    def test_restart_through_leases(self, store_and_ledger):
        """Restart acquires {active, live} from router (FIX B1)."""
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=2)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        # Active source restart.
        new_sid = reserve_start(ledger)
        outcome = mgr.restart(sid, new_session_id=new_sid, force=True, owner_id=OWNER)
        assert outcome.status == "restarted"
        # After restart, new session holds active+live leases.
        assert client.service.active_count == 1
        assert client.service.live_pty_count == 1

    def test_restart_b1_always_acquires_leases(self, store_and_ledger):
        """FIX B1: restart always acquires router leases (both active and terminal paths).

        We test the active-source restart path end-to-end and verify leases are acquired.
        The terminal-source path hits the same ``acquire`` call after resolution.
        """
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=2)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        assert client.service.active_count == 1
        assert client.service.live_pty_count == 1
        new_sid = reserve_start(ledger)
        outcome = mgr.restart(sid, new_session_id=new_sid, force=True, owner_id=OWNER)
        assert outcome.status == "restarted"
        # Old session's leases were released via _free_slot; new session acquired fresh ones.
        assert client.service.active_count == 1
        assert client.service.live_pty_count == 1
        # Verify the old token is gone — restart used a fresh acquire for the new session_id.
        old_active = mgr._active_lease_tokens.get(sid)
        assert old_active is None, "old session's active token should be released"
        new_active = mgr._active_lease_tokens.get(new_sid)
        assert new_active is not None, "new session should have an active token"

    # FIX E: no double-gating on idle-retained cap
    def test_leased_start_ignores_local_idle_retained_cap(self, store_and_ledger):
        """FIX E: leased start is NOT rejected by the generation-local idle-retained cap."""
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=5,
                                                live_pty_limit=5)
        # Override the idle_limit to a very low value.
        mgr._idle_limit = 0
        # Fill idle sessions.
        sid_a = reserve_start(ledger)
        mgr.start(EXECUTOR, "task A", "/tmp", owner_id=OWNER, session_id=sid_a)
        made[0]._control_state = "idle"
        # A second start should succeed because leases bypass local caps.
        sid_b = reserve_start(ledger)
        mgr.start(EXECUTOR, "task B", "/tmp", owner_id=OWNER, session_id=sid_b)
        assert client.service.active_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# S3b: lease reconciliation (§3.3c) — conservative design, no buffer
# ═══════════════════════════════════════════════════════════════════════════════


class TestS3bReconciliation:
    """Conservative reconciliation: router rejects mutations for unreconciled
    epochs. No buffer, no cutoff replay. Generation registers authoritative
    snapshot, then resumes.
    """

    # ── Real restart: no resurrection (fresh second LeaseService) ──────────

    def test_real_restart_no_resurrection(self):
        """Fresh second LeaseService (restarted router). Gen holds token T in
        service A; fresh service B has no tokens, fresh id. Gen acquires with A's
        id, then uses B as restarted router: acquire rejected until snapshot
        registered with B's id; release under B's id succeeds."""
        svc_a = LeaseService(active_limit=5, live_pty_limit=5)
        svc_a.register_snapshot("g-1", "e-1", svc_a.reconciliation_id,
                                cutoff_revision=0, active_tokens=[], live_tokens=[])
        r = svc_a.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                          reconciliation_id=svc_a.reconciliation_id,
                          transition_revision=1)
        tid = r["active"]["token_id"]

        svc_b = LeaseService(active_limit=5, live_pty_limit=5)
        rid_b = svc_b.reconciliation_id
        assert rid_b != svc_a.reconciliation_id

        with pytest.raises(NelixError, match="rebuilding"):
            svc_b.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                          reconciliation_id=rid_b, transition_revision=1)

        active_tokens = [{
            "token_id": tid,
            "key": ("g-1", "e-1", "s-1", "0", "active"),
        }]
        svc_b.register_snapshot("g-1", "e-1", rid_b, cutoff_revision=5,
                                active_tokens=active_tokens, live_tokens=[])
        assert svc_b.active_count == 1

        svc_b.release(tid, reconciliation_id=rid_b, transition_revision=6)
        assert svc_b.active_count == 0

    # ── Cold start: no deadlock ────────────────────────────────────────────

    def test_cold_start_no_deadlock(self):
        """New client registers empty snapshot, then acquires."""
        svc = LeaseService(active_limit=5, live_pty_limit=5)
        rid = svc.reconciliation_id

        with pytest.raises(NelixError, match="rebuilding"):
            svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                        reconciliation_id=rid, transition_revision=1)

        svc.register_snapshot("g-1", "e-1", rid, cutoff_revision=0,
                               active_tokens=[], live_tokens=[])
        svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                    reconciliation_id=rid, transition_revision=2)
        assert svc.active_count == 1

    # ── Late/unregistered epoch → REBUILDING ───────────────────────────────

    def test_unregistered_epoch_rebuilding(self):
        svc = LeaseService(active_limit=10, live_pty_limit=10)
        rid = svc.reconciliation_id

        svc.register_snapshot("g-1", "e-1", rid, cutoff_revision=0,
                               active_tokens=[], live_tokens=[])

        with pytest.raises(NelixError, match="rebuilding"):
            svc.acquire(("g-2", "e-2", "s-1", "0"), {"active"},
                        reconciliation_id=rid, transition_revision=1)

        svc.register_snapshot("g-2", "e-2", rid, cutoff_revision=0,
                               active_tokens=[], live_tokens=[])
        svc.acquire(("g-2", "e-2", "s-1", "0"), {"active"},
                    reconciliation_id=rid, transition_revision=2)
        assert svc.active_count == 1

    # ── Exactly-once handshake + stale-id ──────────────────────────────────

    def test_exactly_once_handshake(self):
        svc = LeaseService(active_limit=10, live_pty_limit=10)
        svc.register_snapshot("g-1", "e-1", svc.reconciliation_id,
                               cutoff_revision=0, active_tokens=[], live_tokens=[])

        svc.set_reconciliation_id(uuid.uuid4().hex)
        new_rid = svc.reconciliation_id

        svc.register_snapshot("g-1", "e-1", new_rid, cutoff_revision=5,
                               active_tokens=[], live_tokens=[])

        result = svc.register_snapshot(
            "g-1", "e-1", new_rid, cutoff_revision=5,
            active_tokens=[], live_tokens=[])
        assert result["acknowledged_revision"] == 5

    def test_stale_reconciliation_id_rejected_on_acquire(self):
        svc = LeaseService()
        old_rid = svc.reconciliation_id
        svc.set_reconciliation_id(uuid.uuid4().hex)
        with pytest.raises(NelixError) as exc:
            svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                        reconciliation_id=old_rid)
        assert exc.value.code == STALE_RECONCILIATION_ID
        assert exc.value.retryable is True

    def test_stale_reconciliation_id_rejected_on_release(self):
        svc = LeaseService()
        r = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        old_rid = svc.reconciliation_id
        tid = r["active"]["token_id"]
        svc.set_reconciliation_id(uuid.uuid4().hex)
        with pytest.raises(NelixError) as exc:
            svc.release(tid, reconciliation_id=old_rid)
        assert exc.value.code == STALE_RECONCILIATION_ID
        assert exc.value.retryable is True

    def test_stale_id_register_snapshot_rejected(self):
        svc = LeaseService()
        svc.set_reconciliation_id(uuid.uuid4().hex)
        with pytest.raises(NelixError) as exc:
            svc.register_snapshot(
                "g-1", "e-1", reconciliation_id="old-rid",
                cutoff_revision=0, active_tokens=[], live_tokens=[])
        assert exc.value.code == STALE_RECONCILIATION_ID

    # ── Outbox: rollover, retry-ack, liveness ──────────────────────────────

    def test_outbox_rollover_rule5(self):
        client = LeaseClient("/nonexistent/sock", timeout=0.1,
                             generation_id="g-1", generation_epoch="e-1")
        client._outbox_add("old-token-1", revision=5, rid="old-rid-1")
        client._outbox_add("old-token-2", revision=8, rid="old-rid-1")
        client._outbox_add("new-token-1", revision=15, rid="new-rid-2")
        assert client.outbox_size() == 3

        drained = client.outbox_drain_upto(10)
        assert drained == 2
        assert client.outbox_size() == 1
        remaining = client.outbox_pending()
        assert "old-token-1" not in remaining
        assert "old-token-2" not in remaining
        assert "new-token-1" in remaining

    def test_outbox_retry_and_ack(self):
        svc = LeaseService(active_limit=5, live_pty_limit=5)
        r = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        tid = r["active"]["token_id"]
        client = LeaseClient("/nonexistent/sock", timeout=0.1,
                             generation_id="g-1", generation_epoch="e-1")
        client._outbox_add(tid, revision=5, rid=None)
        assert client.outbox_size() == 1
        acked = client.outbox_ack(tid)
        assert acked is True
        assert client.outbox_size() == 0

    def test_outbox_liveness_new_id(self):
        """Release under current id works after reconciliation; duplicate
        (idempotent) release returns False without error."""
        svc = LeaseService(active_limit=5, live_pty_limit=5)
        svc.register_snapshot("g-1", "e-1", svc.reconciliation_id,
                               cutoff_revision=0, active_tokens=[], live_tokens=[])
        r = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                        reconciliation_id=svc.reconciliation_id,
                        transition_revision=1)
        tid = r["active"]["token_id"]

        released = svc.release(tid,
                               reconciliation_id=svc.reconciliation_id,
                               transition_revision=6)
        assert released is True
        assert svc.active_count == 0

        released2 = svc.release(tid,
                                reconciliation_id=svc.reconciliation_id,
                                transition_revision=7)
        assert released2 is False

    # ── Malformed snapshot: validate before mutate ─────────────────────────

    def test_malformed_snapshot_no_drift(self):
        svc = LeaseService(active_limit=5, live_pty_limit=5)
        rid = svc.reconciliation_id

        # Each malformed payload targets a FRESH epoch so idempotency does not
        # bypass validation (epoch must not be reconciled yet).
        def _check(payload):
            with pytest.raises(NelixError):
                svc.register_snapshot(
                    "g-new", uuid.uuid4().hex, rid, cutoff_revision=0,
                    active_tokens=payload, live_tokens=[])

        _check(["not-a-dict"])
        _check([{"key": ("g-1", "e-1", "s-1", "0", "active")}])  # no token_id

        # Short key (arity < 5)
        _check([{"token_id": "t1", "key": ("g-1", "e-1")}])

        # Duplicate token_id
        _check([
            {"token_id": "t1", "key": ("g-1", "e-1", "s-1", "0", "active")},
            {"token_id": "t1", "key": ("g-1", "e-1", "s-2", "0", "active")},
        ])

    # ── Kind-key idempotent match ──────────────────────────────────────────

    def test_kind_key_idempotent_match(self):
        svc = LeaseService(active_limit=5, live_pty_limit=5)
        rid = svc.reconciliation_id
        tid = "existing-token"

        svc.register_snapshot("g-1", "e-1", rid, cutoff_revision=0,
                               active_tokens=[{
                                   "token_id": tid,
                                   "key": ("g-1", "e-1", "s-1", "0", "active"),
                               }], live_tokens=[])
        assert svc.active_count == 1

        result = svc.acquire(
            ("g-1", "e-1", "s-1", "0"), {"active"},
            reconciliation_id=rid, transition_revision=1)
        assert result["active"]["token_id"] == tid
        assert result["active"]["fresh"] is False
        assert svc.active_count == 1

    # ── Global bound + registration idempotency ────────────────────────────

    def test_registration_idempotent(self):
        svc = LeaseService(active_limit=10, live_pty_limit=10)
        rid = svc.reconciliation_id

        result = svc.register_snapshot(
            "g-1", "e-1", rid, cutoff_revision=5,
            active_tokens=[], live_tokens=[])
        assert result["acknowledged_revision"] == 5

        result2 = svc.register_snapshot(
            "g-1", "e-1", rid, cutoff_revision=5,
            active_tokens=[], live_tokens=[])
        assert result2["acknowledged_revision"] == 5

    def test_mark_epoch_rebuilding_persists(self):
        svc = LeaseService()
        svc.mark_epoch_rebuilding("g-1", "e-1")
        assert svc.is_epoch_rebuilding("g-1", "e-1") is True

    def test_needs_handshake_rollover_once(self):
        client = LeaseClient("/nonexistent/sock", timeout=0.1,
                             generation_id="g-1", generation_epoch="e-1")
        assert client.needs_handshake() is False
        client.reconciliation_id = "rid-abc"
        assert client.needs_handshake() is True
        client.mark_handshook("rid-abc")
        assert client.needs_handshake() is False
        client.reconciliation_id = "rid-def"
        assert client.needs_handshake() is True
        client.mark_handshook("rid-def")
        assert client.needs_handshake() is False
