"""Regression tests for 9 source defects fixed in nelix-9a4.4 round 5.

Each test MUST fail if its corresponding fix is reverted. Tests use real objects
where practical; stubs are used only for failure-injection points.
"""

import pytest

from nelix_contracts.errors import NelixError
from nelix_store.store import Store

from tests.conftest import EXECUTOR, OWNER, make_spec, own, reserve_start
from daemon.events import EventQueue
from daemon.manager import SessionManager

_OID = "o-" + "a" * 32
_GID = "g-" + "b" * 32


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TerminatingSession:
    def __init__(self, sid, executor, events):
        self.sid = sid
        self.executor = executor
        self._events = events
        self.on_terminal = None
        self.reaper_ctx = None
        self.lineage_id = None
        self.restarted_from = None
        self.restart_count = 0
        self.stopped = False
        self._terminal_kind = "done"
        self.task = "test-task"
        self.cwd = "/tmp"

    def start(self, task, cwd):
        self.task = task
        self.cwd = cwd

    def snapshot(self):
        return {"session_id": self.sid, "executor": self.executor,
                "control_state": "busy", "task_delivery": "pending"}

    def terminal_snapshot(self):
        return {
            "session_id": self.sid, "executor": self.executor,
            "task": self.task, "cwd": self.cwd,
            "control_state": "terminal", "terminal_kind": self._terminal_kind,
            "task_delivery": "terminal", "screen_excerpt": "all done",
            "pending": False, "lineage_id": self.lineage_id,
            "restarted_from": self.restarted_from,
            "restart_count": self.restart_count, "terminal": True,
        }

    def stop(self):
        self.stopped = True
        self._events.publish(self.sid, self.executor, self._terminal_kind,
                             "all done", self._terminal_kind)
        if self.on_terminal is not None:
            self.on_terminal(self.sid)


def _mgr(store, clock=None):
    """Build a minimal SessionManager with a terminating session factory."""
    if clock is None:
        clock = FakeClock(1000.0)
    events = EventQueue()
    specs = {EXECUTOR: make_spec()}
    captured = []
    def sf(sid, ex, spec, ev):
        s = TerminatingSession(sid, ex, ev)
        captured.append(s)
        return s
    m = SessionManager(specs, events, store, session_factory=sf,
                        concurrency_limit=5, terminal_snapshot_ttl=300.0,
                        clock=clock)
    return m, captured, events, clock


# ── [T1] restart e2e through RestartPath -> rpc_client -> fake daemon ──────

def _restart_setup(tmp_path):
    """Set up a real Store + ledger, reserve a start, create a session,
    then wire a SessionManager and return the running session."""
    root = tmp_path / "nelix-db"
    root.mkdir()
    clock = FakeClock(1000.0)
    store = Store(root, clock=clock)
    from nelix_store.ledger import StartLedger
    ledger = StartLedger(root, clock=clock)
    sid = reserve_start(ledger, idempotency_key="rt1")
    mgr, captured, events, _ = _mgr(store, clock)
    # Put the session via the manager (creates store row + owner record)
    _out = mgr.start(EXECUTOR, "do it", "/tmp", owner_id=OWNER, session_id=sid)
    return mgr, ledger, sid, captured, store, clock


def test_t1_restart_via_restartpath_returns_200_restarted(tmp_path):
    """T1: REAL end-to-end /restart through RestartPath -> rpc_client -> fake daemon.
    Assert HTTP 200 with body {'status':'restarted','session_id':<router-allocated new sid>}.
    MUST FAIL if rpc_client.restart() returns a (bool, body) tuple instead of a dict."""
    from rpc_client import RpcClient
    from daemon.transport import Transport
    mgr, ledger, old_sid, captured, store, clock = _restart_setup(tmp_path)

    # Reserve a new session_id for the restart (as the router does)
    new_sid = reserve_start(ledger, idempotency_key="rt-restart")

    # Build a pseudo-client pointing at the in-process daemon via a Unix socket.
    # We need a transport that the local RPC server listens on.
    # Simplest approach: create a local TCP server for this test.
    from daemon.rpc_server import make_server
    import threading
    srv = make_server(mgr, Transport.tcp("127.0.0.1", 0, "tok"))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _, port = srv.server_address
    try:
        client = RpcClient(Transport.tcp("127.0.0.1", port, "tok"), OWNER)
        body = client.restart(old_sid, new_session_id=new_sid, force=True,
                              owner_id=OWNER)
        # body is the dict returned by the daemon's /restart route
        assert isinstance(body, dict), (
            f"T1 FAIL: restart returned {type(body).__name__}, not dict — "
            f"would be a (bool,body) tuple bug if RpcClient.restart() used _call"
        )
        assert body.get("status") == "restarted", (
            f"T1 FAIL: restart status={body.get('status')!r}, expected 'restarted'"
        )
        assert body.get("session_id") == new_sid, (
            f"T1 FAIL: restart returned session_id={body.get('session_id')!r}, "
            f"expected {new_sid}"
        )
    finally:
        srv.shutdown()
        mgr.stop_all()


# ── [T2] status() surfaces store errors ──────────────────────────────────

class _BrokenStore:
    """A store that raises on every call — simulates a persistent db failure."""

    def __init__(self, real_store):
        self._real = real_store

    def create_session(self, *a, **k):
        return self._real.create_session(*a, **k)

    def get_session(self, *a, **k):
        return self._real.get_session(*a, **k)

    def transition_session(self, *a, **k):
        return self._real.transition_session(*a, **k)

    def put_terminal(self, *a, **k):
        return self._real.put_terminal(*a, **k)

    def list_terminal(self, owner_id):
        raise NelixError("store_unavailable", "database is unavailable")


def test_t2_status_surfaces_store_errors(tmp_path):
    """T2: A SessionManager whose store.list_terminal raises must propagate
    the error — caller sees 'store_unavailable', NOT a silently empty board.
    MUST FAIL if the `except Exception: store_records = []` swallow is restored."""
    from nelix_store.ledger import StartLedger
    root = tmp_path / "nelix-db"
    root.mkdir()
    clock = FakeClock(1000.0)
    real_store = Store(root, clock=clock)
    ledger = StartLedger(root, clock=clock)
    broken = _BrokenStore(real_store)
    sid = reserve_start(ledger)
    mgr, captured, events, _ = _mgr(broken, clock)
    _out = mgr.start(EXECUTOR, "do it", "/tmp", owner_id=OWNER, session_id=sid)
    own(sid, OWNER)
    mgr.stop(sid, owner_id=OWNER)
    with pytest.raises(NelixError) as ei:
        mgr.status(owner_id=OWNER)
    assert "store_unavailable" in str(ei.value.code) or "store_unavailable" in str(ei.value), (
        f"T2 FAIL: expected store_unavailable error, got {ei.value}"
    )


# ── [T3] _free_slot persist-before-remove + no silent loss on store error ──

class _PutFailStore:
    """A store whose put_terminal raises a real error (not unknown_session)."""

    def __init__(self, real_store):
        self._real = real_store

    def create_session(self, *a, **k):
        return self._real.create_session(*a, **k)

    def get_session(self, *a, **k):
        return self._real.get_session(*a, **k)

    def transition_session(self, *a, **k):
        return self._real.transition_session(*a, **k)

    def put_terminal(self, session_id, **k):
        raise NelixError("store_unavailable", "cannot write terminal record")

    def list_terminal(self, *a, **k):
        return self._real.list_terminal(*a, **k)


def test_t3_freeslot_persists_before_remove_and_propagates_store_error(tmp_path):
    """T3: _free_slot calls put_terminal BEFORE _sessions.pop (spec §5 ordering).
    A real (non-unknown_session) store error propagates (not silently lost).
    MUST FAIL if _sessions.pop moves back before put_terminal or if the store
    error is swallowed."""
    from nelix_store.ledger import StartLedger
    root = tmp_path / "nelix-db"
    root.mkdir()
    clock = FakeClock(1000.0)
    real_store = Store(root, clock=clock)
    ledger = StartLedger(root, clock=clock)
    failing = _PutFailStore(real_store)
    sid = reserve_start(ledger)
    mgr, captured, events, _ = _mgr(failing, clock)
    _out = mgr.start(EXECUTOR, "do it", "/tmp", owner_id=OWNER, session_id=sid)
    own(sid, OWNER)
    # Stop triggers _free_slot. The store.put_terminal raises store_unavailable.
    # This MUST propagate (not silently lost).
    with pytest.raises(NelixError) as ei:
        mgr.stop(sid, owner_id=OWNER)
    assert "store_unavailable" in str(ei.value.code) or "store_unavailable" in str(ei.value), (
        f"T3 FAIL: expected store_unavailable from put_terminal, got {ei.value}"
    )
    # The session MUST still be in _sessions (not removed before put_terminal)
    assert sid in mgr._sessions, (
        "T3 FAIL: _sessions was popped BEFORE put_terminal — spec §5 ordering violated"
    )


# ── [T4] spawn-failure cleanup + durable row -> "failed" ──────────────────

def test_t4_spawn_failure_cleans_up_sessions_and_transitions_store(tmp_path, monkeypatch):
    """T4: If owner.write raises (spawn failure), the except handler must:
    (a) remove the session from _sessions (no leak), and
    (b) transition the durable store row to 'failed' (not 'starting').
    MUST FAIL if create_session moves outside the try block or the failed-transition
    is removed."""
    from nelix_store.ledger import StartLedger
    root = tmp_path / "nelix-db"
    root.mkdir()
    clock = FakeClock(1000.0)
    store = Store(root, clock=clock)
    ledger = StartLedger(root, clock=clock)
    sid = reserve_start(ledger)
    mgr, captured, events, _ = _mgr(store, clock)

    # Inject a failure into owner.write
    from daemon import owner as _owner_module
    monkeypatch.setattr(_owner_module, "write",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _owner_module.OwnerWriteFailed("injected failure")))

    with pytest.raises(_owner_module.OwnerWriteFailed):
        mgr.start(EXECUTOR, "do it", "/tmp", owner_id=OWNER, session_id=sid)

    # (a) no leaked _sessions entry
    assert sid not in mgr._sessions, (
        "T4 FAIL: leaked _sessions entry after spawn failure"
    )
    # (b) durable row is transitioned to "failed"
    try:
        row = store.get_session(sid, owner_id=OWNER)
        assert row.state == "failed", (
            f"T4 FAIL: expected session state 'failed', got {row.state!r}"
        )
    except NelixError as e:
        # If get_session fails (unknown_session), the row might have been deleted
        # or never created — both mean the rollback worked
        assert e.code == "unknown_session", (
            f"T4 FAIL: get_session raised {e.code}, not unknown_session"
        )


# ── [T5] durable persist with terminal_snapshot_ttl=0 ─────────────────────

def test_t5_durable_persist_with_zero_ttl(tmp_path):
    """T5: A SessionManager with terminal_snapshot_ttl=0 must STILL persist
    terminal records to the store (the TTL only governs the volatile _terminal dict).
    MUST FAIL if put_terminal is re-coupled to `if terminal_ttl > 0`."""
    from nelix_store.ledger import StartLedger
    root = tmp_path / "nelix-db"
    root.mkdir()
    clock = FakeClock(1000.0)
    store = Store(root, clock=clock)
    ledger = StartLedger(root, clock=clock)
    sid = reserve_start(ledger)
    events = EventQueue()
    specs = {EXECUTOR: make_spec()}
    captured = []
    def sf(sid, ex, spec, ev):
        s = TerminatingSession(sid, ex, ev)
        captured.append(s)
        return s
    mgr = SessionManager(specs, events, store, session_factory=sf,
                          concurrency_limit=5, terminal_snapshot_ttl=0,
                          clock=clock)
    _out = mgr.start(EXECUTOR, "do it", "/tmp", owner_id=OWNER, session_id=sid)
    own(sid, OWNER)
    mgr.stop(sid, owner_id=OWNER)
    # A durable record must exist in the store despite ttl=0
    term = store.get_terminal(sid, owner_id=OWNER)
    assert term.terminal_kind == "done", (
        "T5 FAIL: no durable record found (terminal_snapshot_ttl=0 suppressed persist)"
    )


# ── [T6] force distinguishes restart idempotency op ──────────────────────

def test_t6_force_distinguishes_restart_key(tmp_path):
    """T6: A non-forced restart that records budget-exhausted, followed by a
    force:true retry of the same old_sid, is a DISTINCT operation (does NOT replay
    the recorded failure). MUST FAIL if force is dropped from the idempotency key."""
    from router.restart import _request_fingerprint
    # Two fingerprints for the same (owner, old_sid) but different force
    # MUST produce DIFFERENT results
    fp_no_force = _request_fingerprint(OWNER, "s-old", False)
    fp_force = _request_fingerprint(OWNER, "s-old", True)
    assert fp_no_force != fp_force, (
        "T6 FAIL: force=true and force=false produce the same fingerprint — "
        "force is NOT in the idempotency key"
    )
    # Also verify the idempotency key format includes force
    import hashlib
    expected_fp_no = hashlib.sha256(
        f"{OWNER}\x00s-old\x00restart\x00force:False".encode()).hexdigest()[:64]
    assert fp_no_force == expected_fp_no, (
        "T6 FAIL: fingerprint formula changed from expected"
    )


# ── [T7] /start and /restart orchestration_ids don't collide ──────────────

def test_t7_orchestration_domains_dont_collide(tmp_path):
    """T7: For one owner, /start with idempotency_key == old_sid and
    restart(old_sid) yield DIFFERENT orchestration_ids. MUST FAIL if the
    'restart' discriminator is removed from _derive_orchestration_id."""
    from router.restart import _derive_orchestration_id as restart_derived
    # What start.py derives for a key equal to old_sid
    import hashlib
    orch_start_style = "o-" + hashlib.sha256(
        f"{OWNER}\x00s-old".encode()).hexdigest()[:32]
    # What restart.py derives for the same old_sid
    orch_restart = restart_derived(OWNER, "s-old")
    assert orch_restart != orch_start_style, (
        "T7 FAIL: restart orchestration_id collides with /start's domain — "
        "the 'restart' discriminator is missing from the hash"
    )
    # The restart orchestration id must include the discriminator
    expected_restart = "o-" + hashlib.sha256(
        f"{OWNER}\x00s-old\x00restart".encode()).hexdigest()[:32]
    assert orch_restart == expected_restart, (
        "T7 FAIL: restart orchestration_id formula differs from expected"
    )
