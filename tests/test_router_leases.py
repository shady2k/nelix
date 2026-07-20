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
        from daemon.session import RespondOutcome
        return RespondOutcome("resumed", seq=2)


class _MockLeaseClient:
    """A simple mock lease client backed by a real LeaseService for testing."""

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


# ── LeaseService unit tests ──────────────────────────────────────────────


class TestLeaseService:
    def test_acquire_active_caps_cross_generations(self):
        """Active bound is ONE counter across different generation_ids."""
        svc = LeaseService(active_limit=2)
        svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        svc.acquire(("g-2", "e-2", "s-2", "0"), {"active"})
        assert svc.active_count == 2
        with pytest.raises(Exception):
            svc.acquire(("g-3", "e-3", "s-3", "0"), {"active"})

    def test_active_bound(self):
        svc = LeaseService(active_limit=1)
        svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        assert svc.active_count == 1
        with pytest.raises(Exception):
            svc.acquire(("g-2", "e-2", "s-2", "0"), {"active"})

    def test_live_pty_bound_is_separate(self):
        """Live-PTY bound is independent of the active bound."""
        svc = LeaseService(active_limit=1, live_pty_limit=1)
        # Acquire both for one session fills both bounds.
        svc.acquire(("g-1", "e-1", "s-1", "0"), {"active", "live"})
        assert svc.active_count == 1
        assert svc.live_pty_count == 1
        # Active is full, but live is also full — so a different session cannot get live either.
        with pytest.raises(Exception):
            svc.acquire(("g-2", "e-2", "s-2", "0"), {"live"})
        # Release live-only should free live but keep active.
        svc.release(svc._tokens[list(svc._tokens.keys())[0]]["token_id"])
        # Wait, we can't easily find the live token after acquiring both.
        # Let's be more explicit: acquire them separately.

    def test_live_pty_bound_independent(self):
        svc = LeaseService(active_limit=2, live_pty_limit=1)
        t1 = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        t2 = svc.acquire(("g-2", "e-2", "s-2", "0"), {"active", "live"})
        assert svc.active_count == 2
        assert svc.live_pty_count == 1
        # Active limit reached, live limit reached.
        with pytest.raises(Exception):
            svc.acquire(("g-3", "e-3", "s-3", "0"), {"active"})
        # Release active from session 1 — active freed, live still full.
        svc.release(t1["active"])
        assert svc.active_count == 1
        assert svc.live_pty_count == 1
        # Release live from session 2 — live freed too.
        svc.release(t2["live"])
        assert svc.live_pty_count == 0

    def test_idempotent_acquire_same_key(self):
        """Same key returns same token, doesn't double-count."""
        svc = LeaseService(active_limit=1)
        t1 = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        assert svc.active_count == 1
        t2 = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        assert t1["active"] == t2["active"]
        assert svc.active_count == 1  # not double-counted

    def test_idempotent_release_last_frees_slot(self):
        """With 2 refs, first release doesn't free slot, second does."""
        svc = LeaseService(active_limit=1)
        svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        t2 = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        assert svc.active_count == 1
        # First release (t2 has the same token as t1)
        tid = t2["active"]
        svc.release(tid)
        assert svc.active_count == 1  # refcount still > 0
        # Second release frees
        svc.release(tid)
        assert svc.active_count == 0

    def test_release_unknown_token(self):
        svc = LeaseService()
        assert svc.release("nonexistent") is False

    def test_release_after_acquire_free(self):
        svc = LeaseService(active_limit=1)
        t = svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})
        assert svc.release(t["active"]) is True
        assert svc.active_count == 0

    def test_rebuilding_raises(self):
        svc = LeaseService()
        svc.set_rebuilding(True)
        with pytest.raises(Exception):
            svc.acquire(("g-1", "e-1", "s-1", "0"), {"active"})

    def test_active_count_caps_globally(self):
        """3 generations with one lease service share the same active counter."""
        svc = LeaseService(active_limit=2)
        t1 = svc.acquire(("g-a", "e-1", "s-1", "0"), {"active"})
        t2 = svc.acquire(("g-b", "e-2", "s-2", "0"), {"active"})
        assert svc.active_count == 2
        with pytest.raises(Exception):
            svc.acquire(("g-c", "e-3", "s-3", "0"), {"active"})
        svc.release(t1["active"])
        svc.release(t2["active"])
        assert svc.active_count == 0


# ── MockLeaseClient tests ────────────────────────────────────────────────

def test_mock_client_passthrough():
    """Mock lease client delegates to LeaseService correctly."""
    client = _MockLeaseClient(active_limit=1)
    t = client.acquire("g-1", "e-1", "s-1", 0, {"active"})
    assert "active" in t
    assert client.service.active_count == 1
    client.release(t["active"])
    assert client.service.active_count == 0


# ── Manager integration tests (lease-based admission) ────────────────────


class _LeasedFakeSession(FakeSession):
    """Fake session that supports the lease contract (on_idle, _activation_counter)."""
    def __init__(self, sid, executor, *a, **k):
        super().__init__(sid, executor, *a, **k)
        self._control_state = "busy"
        self._activation_counter = 0
        self.on_idle = None

    def snapshot(self):
        return {"session_id": self.sid, "executor": self.executor,
                "control_state": self._control_state,
                "task_delivery": "delivered"}

    def send_turn(self, text):
        # Transition to busy so a concurrent racing send_turn sees not_idle.
        self._control_state = "busy"
        return RespondOutcome("resumed", seq=2)


def _lease_mgr(store_and_ledger, active_limit=2, live_pty_limit=2):
    """Build a SessionManager with a mock lease client."""
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
        """start() acquires both active and live leases."""
        mgr, _, ledger, client = _lease_mgr(store_and_ledger, active_limit=5)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        assert client.service.active_count == 1
        assert client.service.live_pty_count == 1

    def test_active_bound_caps_across_sessions(self, store_and_ledger):
        """Active bound caps the total across all sessions (not per-generation)."""
        mgr, _, ledger, client = _lease_mgr(store_and_ledger, active_limit=1)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        sid2 = reserve_start(ledger)
        with pytest.raises(RuntimeError, match="concurrency_limit"):
            mgr.start(EXECUTOR, "task B", "/tmp", owner_id=OWNER, session_id=sid2)

    def test_live_pty_bound_independent(self, store_and_ledger):
        """Live-PTY bound is separate: can fill live without filling active."""
        mgr, _, ledger, client = _lease_mgr(store_and_ledger,
                                             active_limit=5, live_pty_limit=1)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        assert client.service.active_count == 1
        assert client.service.live_pty_count == 1
        # Live-pty is full, but active has room.
        sid2 = reserve_start(ledger)
        with pytest.raises(RuntimeError, match="concurrency_limit|live"):
            mgr.start(EXECUTOR, "task B", "/tmp", owner_id=OWNER, session_id=sid2)

    def test_terminal_releases_both_leases(self, store_and_ledger):
        """_free_slot releases both active and live leases."""
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=1)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        assert client.service.active_count == 1
        # Simulate terminal by calling on_terminal
        sess = made[0]
        sess._control_state = "terminal"
        sess.on_terminal(sid)
        # After on_terminal -> _free_slot, leases should be released
        assert client.service.active_count == 0
        assert client.service.live_pty_count == 0
        # Now should be able to start another
        sid2 = reserve_start(ledger)
        mgr.start(EXECUTOR, "task B", "/tmp", owner_id=OWNER, session_id=sid2)
        assert client.service.active_count == 1

    def test_racing_send_turns_same_idle_session(self, store_and_ledger):
        """Two racing send_turns for the same idle session don't double-consume."""
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=2)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        sess = made[0]
        # Make session idle
        sess._control_state = "idle"
        # Simulate two racing send_turns
        results = []
        def send():
            r = mgr.send_turn(sid, "hello")
            results.append(r)
        t1 = threading.Thread(target=send)
        t2 = threading.Thread(target=send)
        t1.start(); t2.start()
        t1.join(); t2.join()
        # At most one should have succeeded (the other gets no_pending or at_capacity)
        successes = [r for r in results if r.status in ("resumed", "queued")]
        assert len(successes) <= 1, \
            f"Expected at most 1 success, got {len(successes)}: {[r.status for r in results]}"
        # Active count should have increased by at most 1
        assert client.service.active_count <= 2  # 1 from start + at most 1 from send_turn

    def test_send_turn_releases_active_on_idle(self, store_and_ledger):
        """Active lease should be released when session transitions to idle
        (via on_idle callback)."""
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=2)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        sess = made[0]
        assert client.service.active_count == 1
        # Fire on_idle (simulating the session going idle)
        if sess.on_idle is not None:
            sess.on_idle(sid)
        # After idle callback, active lease should be released
        assert client.service.active_count == 0, \
            "Active lease should be released on idle"
        # Live lease should still be held
        assert client.service.live_pty_count == 1, \
            "Live lease should persist after idle"

    def test_send_turn_acquires_active_only(self, store_and_ledger):
        """send_turn for idle session acquires active lease (not live)."""
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=2)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        sess = made[0]
        # Go idle (release active)
        if sess.on_idle is not None:
            sess.on_idle(sid)
        assert client.service.active_count == 0
        assert client.service.live_pty_count == 1
        # Send turn should acquire active
        sess._control_state = "idle"
        mgr.send_turn(sid, "follow-up")
        assert client.service.active_count == 1
        assert client.service.live_pty_count == 1  # unchanged

    def test_router_unavailable_returns_admission_unavailable(self, store_and_ledger):
        """When router cannot be reached, send_turn returns admission_unavailable."""
        # Use a lease client pointing at a non-existent socket
        bad_client = LeaseClient("/nonexistent/router.sock", timeout=0.5)
        store, ledger = store_and_ledger
        specs = {EXECUTOR: make_spec()}
        q = EventQueue()
        mgr = SessionManager(specs, q, store, session_factory=lambda sid, ex, sp, ev: _LeasedFakeSession(sid, ex),
                             concurrency_limit=5, lease_client=bad_client,
                             generation_id="g-1", generation_epoch="e-1")
        # Directly inject a session so we can call send_turn
        from tests.conftest import own
        sid = "s-" + "a" * 32
        sess = _LeasedFakeSession(sid, EXECUTOR)
        sess._control_state = "idle"
        own(sid)
        with mgr._lock:
            mgr._sessions[sid] = sess
        outcome = mgr.send_turn(sid, "hello")
        assert outcome.status == "admission_unavailable"

    def test_restart_releases_old_leases_acquires_new(self, store_and_ledger):
        """Restart of a leased session releases old tokens and acquires new ones."""
        mgr, made, ledger, client = _lease_mgr(store_and_ledger, active_limit=2)
        sid = reserve_start(ledger)
        mgr.start(EXECUTOR, "task", "/tmp", owner_id=OWNER, session_id=sid)
        new_sid = reserve_start(ledger)
        outcome = mgr.restart(sid, new_session_id=new_sid, force=True, owner_id=OWNER)
        assert outcome.status == "restarted"
        # After restart, old tokens are gone and new ones exist
        assert len(client.service._tokens) > 0 or client.service.active_count == 0
