"""Tests for nelix-9a4.4: durable terminal records — the FIRST WRITER of nelix_store.

The live defect: terminal_snapshot_ttl=300.0 sweeps terminal results on every global
status — harness away 6 minutes and the result is GONE from the board. The durable
answer: persist terminal records to nelix_store before removing the live session.
"""

from nelix_store.ledger import StartLedger
from nelix_store.store import Store

from tests.conftest import EXECUTOR, OWNER, make_spec, own
from daemon.events import EventQueue
from daemon.manager import SessionManager


class FakeClock:
    """A clock the test advances at will, so no real sleep is ever needed."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TerminatingSession:
    """A FakeSession whose stop() drives the real terminal path: publish a terminal
    event, then invoke on_terminal (which calls manager._free_slot) — exactly the
    sequence the real monitor's _finish_publish + _finish_cleanup follow."""

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


def _setup_store_and_ledger(tmp_path, clock):
    """Create a Store + StartLedger sharing the same database root."""
    root = tmp_path / "nelix-db"
    root.mkdir()
    store = Store(root, clock=clock)
    ledger = StartLedger(root, clock=clock)
    return store, ledger


_OID = "o-" + "a" * 32
_GID = "g-" + "b" * 32
_GEPOCH = "g-" + "c" * 32


def _router_started_session(ledger, store, owner_id=OWNER, clock=None):
    """Reserve a start via the ledger (as the router would), assign a generation,
    then return the session_id — exactly the state a router-supplied session
    carries when it reaches the daemon."""
    r = ledger.reserve(idempotency_key="k1", owner_id=owner_id,
                       orchestration_id=_OID, request_fingerprint="fp1")
    ledger.assign_generation(r.session_id, _GID, _GEPOCH)
    return r.session_id


def _mgr_with_store(tmp_path, clock, store, events=None):
    """Build a SessionManager with the store wired in, plus a terminating
    session factory."""
    if events is None:
        events = EventQueue()
    specs = {EXECUTOR: make_spec()}
    captured = []

    def session_factory(sid, executor, spec, ev):
        s = TerminatingSession(sid, executor, ev)
        captured.append(s)
        return s

    mgr = SessionManager(specs, events, store, session_factory=session_factory,
                         concurrency_limit=5, terminal_snapshot_ttl=300.0,
                         clock=clock)
    return mgr, captured


# ── TDD: failing tests first ──────────────────────────────────────────────


def test_terminal_persisted_to_store_on_free_slot(tmp_path):
    """When a session terminates via _free_slot, its terminal record MUST be
    persisted to nelix_store BEFORE the live session is removed."""
    clock = FakeClock(1000.0)
    store, ledger = _setup_store_and_ledger(tmp_path, clock)
    sid = _router_started_session(ledger, store)

    events = EventQueue()
    mgr, captured = _mgr_with_store(tmp_path, clock, store, events)

    # Start the session — store.create_session() reads identity from the start row
    _out = mgr.start(EXECUTOR, "do it", "/tmp", owner_id=OWNER,
                     session_id=sid)
    assert _out.session_id == sid
    assert sid in mgr._sessions

    # Assert the session was created in the store
    stored = store.get_session(sid, owner_id=OWNER)
    assert stored.session_id == sid
    assert stored.owner_id == OWNER
    assert stored.executor == EXECUTOR
    assert stored.task == "do it"

    # Stop the session, which triggers on_terminal -> _free_slot
    mgr.stop(sid, owner_id=OWNER)

    # The terminal record MUST be in the store now (the live defect fix)
    term = store.get_terminal(sid, owner_id=OWNER)
    assert term.terminal_kind == "done"
    assert term.session_id == sid
    assert term.summary == "all done"
    # published_at is the STORE's clock, not the caller's
    assert term.published_at == 1000.0
    # The result is on the board (unacknowledged, not expired)
    board = store.list_terminal(OWNER)
    assert len(board) == 1
    assert board[0].session_id == sid


def test_durable_record_survives_ttl_expiry(tmp_path):
    """THE LIVE DEFECT FIX: after terminal_snapshot_ttl (300s), the result
    MUST still be board-visible from the store. A harness away past the TTL
    must still see the terminal result."""
    clock = FakeClock(1000.0)
    store, ledger = _setup_store_and_ledger(tmp_path, clock)
    sid = _router_started_session(ledger, store)

    events = EventQueue()
    mgr, captured = _mgr_with_store(tmp_path, clock, store, events)

    # Start and stop a session
    mgr.start(EXECUTOR, "do it", "/tmp", owner_id=OWNER,
              session_id=sid)
    # Write the owner record (a real start does this; our test bypasses _spawn's
    # owner.write call)
    own(sid, OWNER)
    mgr.stop(sid, owner_id=OWNER)

    # Verify terminal is in volatile dict (TTL window)
    status0 = mgr.status(owner_id=OWNER)
    assert sid in status0["recent_terminal"], \
        "terminal should be in recent_terminal while TTL is fresh"

    # Advance clock past the TTL (300s)
    clock.t = 2000.0

    # status() sweeps expired terminals from the volatile dict
    status1 = mgr.status(owner_id=OWNER)

    # The volatile entry is gone (expired)
    assert sid not in mgr._terminal, \
        "volatile terminal should be swept after TTL expiry"

    # BUT the store-backed result is STILL board-visible
    # After TTL expiry, status() MUST supplement recent_terminal from the store
    assert sid in status1["recent_terminal"], (
        f"BUG REGRESSION: harness away {clock.t - 1000.0}s past the TTL — "
        f"terminal result is GONE from the board. The store has it "
        f"(list_terminal={[t.session_id for t in store.list_terminal(OWNER)]}) "
        f"but status() did not surface it."
    )

    # Verify the store still has the record
    term = store.get_terminal(sid, owner_id=OWNER)
    assert term.terminal_kind == "done"


def test_status_merges_store_and_volatile_without_duplicates(tmp_path):
    """When a terminal is both in the volatile dict (fresh) AND the store,
    status() must not return duplicate entries."""
    clock = FakeClock(1000.0)
    store, ledger = _setup_store_and_ledger(tmp_path, clock)
    sid = _router_started_session(ledger, store)

    events = EventQueue()
    mgr, captured = _mgr_with_store(tmp_path, clock, store, events)

    mgr.start(EXECUTOR, "do it", "/tmp", owner_id=OWNER,
              session_id=sid)
    own(sid, OWNER)
    mgr.stop(sid, owner_id=OWNER)

    # Fresh: volatile dict has it, store has it — status must deduplicate
    status = mgr.status(owner_id=OWNER)
    assert sid in status["recent_terminal"]
    # No double-counting
    assert len([k for k in status["recent_terminal"] if k == sid]) == 1
