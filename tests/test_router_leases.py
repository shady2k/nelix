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
    ADMISSION_UNAVAILABLE, REBUILDING, STALE_RECONCILIATION_ID, NelixError,
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
    """Mock lease client backed by a real LeaseService for testing.

    Auto-reconciles any epoch on first use and tracks token -> epoch
    mapping for releases.
    """

    def __init__(self, active_limit=5, live_pty_limit=5):
        self._service = LeaseService(active_limit=active_limit,
                                      live_pty_limit=live_pty_limit)
        self._gen_id = "g-" + uuid.uuid4().hex
        self._gen_epoch = "g-" + uuid.uuid4().hex
        self._token_epoch = {}

    @property
    def service(self):
        return self._service

    def acquire(self, generation_id, generation_epoch, session_id,
                activation_id, kinds):
        gen_id = str(generation_id)
        gen_ep = str(generation_epoch)
        rid = self._service.reconciliation_id
        self._service.register_snapshot(gen_id, gen_ep, rid, [], [])
        key = (gen_id, gen_ep, str(session_id), str(activation_id))
        result = self._service.acquire(key, kinds, reconciliation_id=rid)
        for kind_key, info in result.items():
            tid = info.get("token_id") if isinstance(info, dict) else info
            if tid and info.get("fresh", True):
                self._token_epoch[tid] = (gen_id, gen_ep)
        return result

    def release(self, token_id):
        ep = self._token_epoch.pop(token_id, (self._gen_id, self._gen_epoch))
        gen_id, gen_ep = ep
        rid = self._service.reconciliation_id
        self._service.register_snapshot(gen_id, gen_ep, rid, [], [])
        return self._service.release(gen_id, gen_ep, token_id,
                                      reconciliation_id=rid)

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
    return info["token_id"] if isinstance(info, dict) else info


# ── LeaseService unit tests ──────────────────────────────────────────────


class TestLeaseService:
    def test_acquire_active_caps_cross_generations(self):
        svc = LeaseService(active_limit=2)
        rid = svc.reconciliation_id
        svc.register_snapshot("g-1", "e-1", rid, [], [])
        svc.register_snapshot("g-2", "e-2", rid, [], [])
        svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"}, reconciliation_id=rid)
        svc.acquire(("g-2", "e-2", "s-2", "0"), {"active"}, reconciliation_id=rid)
        assert svc.active_count == 2
        with pytest.raises(NelixError):
            svc.acquire(("g-3", "e-3", "s-3", "0"), {"active"},
                        reconciliation_id=rid)

    def test_active_bound(self):
        svc = LeaseService(active_limit=1)
        rid = svc.reconciliation_id
        svc.register_snapshot("g-1", "e-1", rid, [], [])
        svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"}, reconciliation_id=rid)
        assert svc.active_count == 1
        with pytest.raises(NelixError):
            svc.acquire(("g-2", "e-2", "s-2", "0"), {"active"},
                        reconciliation_id=rid)

    def test_live_pty_bound_independent(self):
        svc = LeaseService(active_limit=2, live_pty_limit=1)
        rid = svc.reconciliation_id
        svc.register_snapshot("g-1", "e-1", rid, [], [])
        svc.register_snapshot("g-2", "e-2", rid, [], [])
        svc.register_snapshot("g-3", "e-3", rid, [], [])
        t1 = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                         reconciliation_id=rid)
        t2 = svc.acquire(("g-2", "e-2", "s-2", "0"), {"active", "live"},
                         reconciliation_id=rid)
        assert svc.active_count == 2
        assert svc.live_pty_count == 1
        with pytest.raises(NelixError):
            svc.acquire(("g-3", "e-3", "s-3", "0"), {"active"},
                        reconciliation_id=rid)
        svc.release("g-1", "e-1", _tid(t1["active"]), reconciliation_id=rid)
        assert svc.active_count == 1
        assert svc.live_pty_count == 1
        svc.release("g-2", "e-2", _tid(t2["live"]), reconciliation_id=rid)
        assert svc.live_pty_count == 0

    def test_acquire_atomic_no_leak_on_live_cap(self):
        svc = LeaseService(active_limit=2, live_pty_limit=1)
        rid = svc.reconciliation_id
        svc.register_snapshot("g-1", "e-1", rid, [], [])
        svc.register_snapshot("g-2", "e-2", rid, [], [])
        svc.acquire(("g-1", "e-1", "s-1", "0"), {"live"}, reconciliation_id=rid)
        assert svc.active_count == 0
        assert svc.live_pty_count == 1
        with pytest.raises(NelixError, match="live"):
            svc.acquire(("g-2", "e-2", "s-2", "0"), {"active", "live"},
                        reconciliation_id=rid)
        assert svc.active_count == 0
        assert svc.live_pty_count == 1

    def test_idempotent_acquire_same_key(self):
        svc = LeaseService(active_limit=1)
        rid = svc.reconciliation_id
        svc.register_snapshot("g-1", "e-1", rid, [], [])
        r1 = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                         reconciliation_id=rid)
        assert svc.active_count == 1
        assert r1["active"]["fresh"] is True
        r2 = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                         reconciliation_id=rid)
        assert r2["active"]["token_id"] == r1["active"]["token_id"]
        assert r2["active"]["fresh"] is False
        assert svc.active_count == 1

    def test_release_exactly_once_no_undercount(self):
        svc = LeaseService(active_limit=1)
        rid = svc.reconciliation_id
        svc.register_snapshot("g-1", "e-1", rid, [], [])
        r = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                        reconciliation_id=rid)
        tid = _tid(r["active"])
        assert svc.active_count == 1
        assert svc.release("g-1", "e-1", tid, reconciliation_id=rid) is True
        assert svc.active_count == 0
        assert svc.release("g-1", "e-1", tid, reconciliation_id=rid) is False
        assert svc.active_count == 0

    def test_lost_acquire_response_retry_no_leak(self):
        svc = LeaseService(active_limit=1)
        rid = svc.reconciliation_id
        svc.register_snapshot("g-1", "e-1", rid, [], [])
        r1 = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                         reconciliation_id=rid)
        tid = _tid(r1["active"])
        assert svc.active_count == 1
        r2 = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                         reconciliation_id=rid)
        assert r2["active"]["fresh"] is False
        assert svc.active_count == 1
        assert svc.release("g-1", "e-1", tid, reconciliation_id=rid) is True
        assert svc.active_count == 0

    def test_release_unknown_token(self):
        svc = LeaseService()
        rid = svc.reconciliation_id
        svc.register_snapshot("g-1", "e-1", rid, [], [])
        assert svc.release("g-1", "e-1", "nonexistent",
                           reconciliation_id=rid) is False

    def test_release_after_acquire_free(self):
        svc = LeaseService(active_limit=1)
        rid = svc.reconciliation_id
        svc.register_snapshot("g-1", "e-1", rid, [], [])
        r = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                        reconciliation_id=rid)
        assert svc.release("g-1", "e-1", _tid(r["active"]),
                           reconciliation_id=rid) is True
        assert svc.active_count == 0

    def test_rebuilding_raises(self):
        svc = LeaseService()
        rid = svc.reconciliation_id
        svc.register_snapshot("g-1", "e-1", rid, [], [])
        svc.set_rebuilding(True)
        with pytest.raises(NelixError):
            svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                        reconciliation_id=rid)


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
        assert len(successes) <= 1
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

    def test_restart_through_leases(self, store_and_ledger):
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=2)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        new_sid = reserve_start(ledger)
        outcome = mgr.restart(sid, new_session_id=new_sid, force=True, owner_id=OWNER)
        assert outcome.status == "restarted"
        assert client.service.active_count == 1
        assert client.service.live_pty_count == 1

    def test_restart_b1_always_acquires_leases(self, store_and_ledger):
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=2)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        assert client.service.active_count == 1
        assert client.service.live_pty_count == 1
        new_sid = reserve_start(ledger)
        outcome = mgr.restart(sid, new_session_id=new_sid, force=True, owner_id=OWNER)
        assert outcome.status == "restarted"
        assert client.service.active_count == 1
        assert client.service.live_pty_count == 1
        old_active = mgr._active_lease_tokens.get(sid)
        assert old_active is None
        new_active = mgr._active_lease_tokens.get(new_sid)
        assert new_active is not None

    def test_leased_start_ignores_local_idle_retained_cap(self, store_and_ledger):
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=5,
                                                live_pty_limit=5)
        mgr._idle_limit = 0
        sid_a = reserve_start(ledger)
        mgr.start(EXECUTOR, "task A", "/tmp", owner_id=OWNER, session_id=sid_a)
        made[0]._control_state = "idle"
        sid_b = reserve_start(ledger)
        mgr.start(EXECUTOR, "task B", "/tmp", owner_id=OWNER, session_id=sid_b)
        assert client.service.active_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# S3b: lease reconciliation — conservative design, no buffer
# ═══════════════════════════════════════════════════════════════════════════════


class TestS3bReconciliation:

    # ── Real restart: no resurrection (fresh second LeaseService) ──────────

    def test_real_restart_no_resurrection(self):
        """Fresh second LeaseService (restarted router). Gen holds token T,
        registers snapshot under B's id, release succeeds."""
        svc_a = LeaseService(active_limit=5, live_pty_limit=5)
        rid_a = svc_a.reconciliation_id
        svc_a.register_snapshot("g-1", "e-1", rid_a, [], [])
        r = svc_a.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                          reconciliation_id=rid_a)
        tid = r["active"]["token_id"]

        svc_b = LeaseService(active_limit=5, live_pty_limit=5)
        rid_b = svc_b.reconciliation_id

        with pytest.raises(NelixError, match="rebuilding"):
            svc_b.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                          reconciliation_id=rid_b)

        active_tokens = [{
            "token_id": tid,
            "key": ("g-1", "e-1", "s-1", "0", "active"),
        }]
        svc_b.register_snapshot("g-1", "e-1", rid_b, active_tokens, [])
        assert svc_b.active_count == 1

        svc_b.release("g-1", "e-1", tid, reconciliation_id=rid_b)
        assert svc_b.active_count == 0

    # ── FIX 1 race: release before registration ────────────────────────────

    def test_real_restart_release_races_registration(self):
        """Release sent BEFORE registration on a fresh router raises
        REBUILDING (not False), so the outbox keeps the entry. After
        registration, retry release succeeds. T never resurrected."""
        svc_a = LeaseService(active_limit=5, live_pty_limit=5)
        rid_a = svc_a.reconciliation_id
        svc_a.register_snapshot("g-1", "e-1", rid_a, [], [])
        r = svc_a.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                          reconciliation_id=rid_a)
        tid = r["active"]["token_id"]

        svc_b = LeaseService(active_limit=5, live_pty_limit=5)
        rid_b = svc_b.reconciliation_id

        # Release arrives at svc_b BEFORE registration -> REBUILDING.
        with pytest.raises(NelixError) as exc:
            svc_b.release("g-1", "e-1", tid, reconciliation_id=rid_b)
        assert exc.value.code == REBUILDING

        # Register snapshot including T.
        active_tokens = [{
            "token_id": tid,
            "key": ("g-1", "e-1", "s-1", "0", "active"),
        }]
        svc_b.register_snapshot("g-1", "e-1", rid_b, active_tokens, [])
        assert svc_b.active_count == 1

        # Retry release under reconciled epoch -> freed.
        released = svc_b.release("g-1", "e-1", tid, reconciliation_id=rid_b)
        assert released is True
        assert svc_b.active_count == 0

        # T never resurrected — release was the definitive ack.
        released2 = svc_b.release("g-1", "e-1", tid, reconciliation_id=rid_b)
        assert released2 is False

    # ── Cold start ─────────────────────────────────────────────────────────

    def test_cold_start_no_deadlock(self):
        svc = LeaseService(active_limit=5, live_pty_limit=5)
        rid = svc.reconciliation_id

        with pytest.raises(NelixError, match="rebuilding"):
            svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                        reconciliation_id=rid)

        svc.register_snapshot("g-1", "e-1", rid, [], [])
        svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                    reconciliation_id=rid)
        assert svc.active_count == 1

    # ── Late epoch ─────────────────────────────────────────────────────────

    def test_unregistered_epoch_rebuilding(self):
        svc = LeaseService(active_limit=10, live_pty_limit=10)
        rid = svc.reconciliation_id
        svc.register_snapshot("g-1", "e-1", rid, [], [])

        with pytest.raises(NelixError, match="rebuilding"):
            svc.acquire(("g-2", "e-2", "s-1", "0"), {"active"},
                        reconciliation_id=rid)

        svc.register_snapshot("g-2", "e-2", rid, [], [])
        svc.acquire(("g-2", "e-2", "s-1", "0"), {"active"},
                    reconciliation_id=rid)
        assert svc.active_count == 1

    # ── Service-level mandatory id (FIX 7) ─────────────────────────────────

    def test_service_rejects_missing_id(self):
        """acquire with reconciliation_id=None on unreconciled epoch raises
        STALE_RECONCILIATION_ID (not REBUILDING)."""
        svc = LeaseService()
        with pytest.raises(NelixError) as exc:
            svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        assert exc.value.code == STALE_RECONCILIATION_ID
        assert exc.value.retryable is True

    # ── Exactly-once handshake + captured id (FIX 2) ──────────────────────

    def test_handshake_exactly_once_captured_id(self):
        """register_snapshot uses the captured target_rid, not the current
        client id; a second registration for the same id is idempotent."""
        svc = LeaseService(active_limit=10, live_pty_limit=10)
        svc.register_snapshot("g-1", "e-1", svc.reconciliation_id, [], [])

        svc.set_reconciliation_id(uuid.uuid4().hex)
        new_rid = svc.reconciliation_id

        # Thread the captured target_rid into register_snapshot.
        result = svc.register_snapshot("g-1", "e-1", new_rid, [], [])
        assert result["reconciliation_id"] == new_rid

        # Second registration with same id is idempotent.
        result2 = svc.register_snapshot("g-1", "e-1", new_rid, [], [])
        assert result2["reconciliation_id"] == new_rid

    # ── Stale id rejects ───────────────────────────────────────────────────

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
        rid = svc.reconciliation_id
        svc.register_snapshot("g-1", "e-1", rid, [], [])
        r = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                        reconciliation_id=rid)
        tid = r["active"]["token_id"]
        old_rid = svc.reconciliation_id
        svc.set_reconciliation_id(uuid.uuid4().hex)
        with pytest.raises(NelixError) as exc:
            svc.release("g-1", "e-1", tid, reconciliation_id=old_rid)
        assert exc.value.code == STALE_RECONCILIATION_ID
        assert exc.value.retryable is True

    def test_stale_id_register_snapshot_rejected(self):
        svc = LeaseService()
        svc.set_reconciliation_id(uuid.uuid4().hex)
        with pytest.raises(NelixError) as exc:
            svc.register_snapshot(
                "g-1", "e-1", reconciliation_id="old-rid",
                active_tokens=[], live_tokens=[])
        assert exc.value.code == STALE_RECONCILIATION_ID

    # ── Outbox ─────────────────────────────────────────────────────────────

    def test_outbox_retry_and_ack(self):
        svc = LeaseService(active_limit=5, live_pty_limit=5)
        rid = svc.reconciliation_id
        svc.register_snapshot("g-1", "e-1", rid, [], [])
        r = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                        reconciliation_id=rid)
        tid = r["active"]["token_id"]
        client = LeaseClient("/nonexistent/sock", timeout=0.1,
                             generation_id="g-1", generation_epoch="e-1")
        client._outbox_add(tid)
        assert client.outbox_size() == 1
        acked = client.outbox_ack(tid)
        assert acked is True
        assert client.outbox_size() == 0

    def test_outbox_liveness_new_id(self):
        svc = LeaseService(active_limit=5, live_pty_limit=5)
        rid = svc.reconciliation_id
        svc.register_snapshot("g-1", "e-1", rid, [], [])
        r = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"},
                        reconciliation_id=rid)
        tid = r["active"]["token_id"]

        released = svc.release("g-1", "e-1", tid, reconciliation_id=rid)
        assert released is True
        assert svc.active_count == 0

        released2 = svc.release("g-1", "e-1", tid, reconciliation_id=rid)
        assert released2 is False

    # ── Malformed snapshot (FIX 5) ─────────────────────────────────────────

    def test_malformed_snapshot_no_drift(self):
        svc = LeaseService(active_limit=5, live_pty_limit=5)
        rid = svc.reconciliation_id

        def _check(payload):
            with pytest.raises(NelixError):
                svc.register_snapshot(
                    "g-new", uuid.uuid4().hex, rid,
                    payload, [])

        _check(["not-a-dict"])
        _check([{"key": ("g-1", "e-1", "s-1", "0", "active")}])

        # Key arity < 5
        _check([{"token_id": "t1", "key": ("g-1", "e-1")}])

        # Duplicate token_id
        _check([
            {"token_id": "t1", "key": ("g-1", "e-1", "s-1", "0", "active")},
            {"token_id": "t1", "key": ("g-1", "e-1", "s-2", "0", "active")},
        ])

        # Activation_id not a string
        _check([{"token_id": "t1", "key": ("g-1", "e-1", "s-1", 0, "active")}])

    def test_snapshot_rejects_noncanonical_key(self):
        """An active entry whose key ends in 'live' is rejected."""
        svc = LeaseService(active_limit=5, live_pty_limit=5)
        rid = svc.reconciliation_id
        with pytest.raises(NelixError, match="does not match expected"):
            svc.register_snapshot("g-1", "e-1", rid, [
                {"token_id": "t1",
                 "key": ("g-1", "e-1", "s-1", "0", "live")},
            ], [])

        # Key gen/epoch mismatch
        with pytest.raises(NelixError, match="does not match epoch"):
            svc.register_snapshot("g-1", "e-1", rid, [
                {"token_id": "t1",
                 "key": ("g-2", "e-2", "s-1", "0", "active")},
            ], [])

    # ── Kind-key idempotent match ──────────────────────────────────────────

    def test_kind_key_idempotent_match(self):
        svc = LeaseService(active_limit=5, live_pty_limit=5)
        rid = svc.reconciliation_id
        tid = "existing-token"

        svc.register_snapshot("g-1", "e-1", rid, [{
            "token_id": tid,
            "key": ("g-1", "e-1", "s-1", "0", "active"),
        }], [])
        assert svc.active_count == 1

        result = svc.acquire(
            ("g-1", "e-1", "s-1", "0"), {"active"},
            reconciliation_id=rid)
        assert result["active"]["token_id"] == tid
        assert result["active"]["fresh"] is False
        assert svc.active_count == 1

    # ── Registration idempotency ───────────────────────────────────────────

    def test_registration_idempotent(self):
        svc = LeaseService(active_limit=10, live_pty_limit=10)
        rid = svc.reconciliation_id

        result = svc.register_snapshot("g-1", "e-1", rid, [], [])
        assert result["reconciliation_id"] == rid

        result2 = svc.register_snapshot("g-1", "e-1", rid, [], [])
        assert result2["reconciliation_id"] == rid

    # ── Mark epoch rebuilding ──────────────────────────────────────────────

    def test_mark_epoch_rebuilding_persists(self):
        svc = LeaseService()
        svc.mark_epoch_rebuilding("g-1", "e-1")
        assert svc.is_epoch_rebuilding("g-1", "e-1") is True

    # ── Handshake rollover ─────────────────────────────────────────────────

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
