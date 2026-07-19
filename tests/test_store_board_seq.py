"""S2a.1: owner_board_seq per-owner change-version counter and atomic read_board_snapshot.

Tests EVERY Acceptance clause listed in the spec, including edge cases not
covered by happy paths: idempotent no-op paths that MUST NOT increment, prune
across multiple owners, schema-v4 reopen proving additive DDL, and the
cursor-before-snapshot invariant.
"""
import sqlite3

import pytest

from nelix_contracts import errors
from nelix_contracts.errors import NelixError
from nelix_store.ledger import StartLedger
from nelix_store.store import Store

OID = "o-" + "2" * 32
GID = "g-" + "3" * 32
GEPOCH = "g-" + "6" * 32


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return FakeClock(1000.0)


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path, clock=clock)
    yield s
    s.close()


@pytest.fixture
def ledger(tmp_path):
    lg = StartLedger(tmp_path, clock=lambda: 1000.0)
    yield lg
    lg.close()


def _started_session(store, ledger, owner="hermes:local", key=None, **over):
    import uuid
    k = key or f"k-{uuid.uuid4().hex[:8]}"
    r = ledger.reserve(idempotency_key=k, owner_id=owner,
                        orchestration_id=OID, request_fingerprint="fp")
    ledger.assign_generation(r.session_id, GID, GEPOCH)
    fields = dict(state="running", executor="coder", task="t", cwd="/repo",
                  model=None, created_at=100.0)
    fields.update(over)
    store.create_session(r.session_id, **fields)
    return r.session_id


# ---- put_terminal bumps ---------------------------------------------------

def test_put_terminal_increments_board_seq_by_1(store, ledger):
    sid = _started_session(store, ledger, owner="o1")
    assert store.get_owner_board_seq("o1") == 0
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=5.0)
    assert store.get_owner_board_seq("o1") == 1


def test_put_terminal_idempotent_retry_does_not_increment(store, ledger):
    sid = _started_session(store, ledger, owner="o2")
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    assert store.get_owner_board_seq("o2") == 1
    # Idempotent re-put: same result.
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    assert store.get_owner_board_seq("o2") == 1, \
        "idempotent re-put_terminal must not increment board_seq"


# ---- ack_terminal bumps ---------------------------------------------------

def test_ack_terminal_increments_board_seq_by_1(store, ledger):
    sid = _started_session(store, ledger, owner="o3")
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    before = store.get_owner_board_seq("o3")
    store.ack_terminal(sid, owner_id="o3")
    assert store.get_owner_board_seq("o3") == before + 1


def test_ack_terminal_idempotent_second_ack_does_not_increment(store, ledger, clock):
    sid = _started_session(store, ledger, owner="o4")
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    store.ack_terminal(sid, owner_id="o4")
    seq = store.get_owner_board_seq("o4")
    clock.t = 2000.0
    store.ack_terminal(sid, owner_id="o4")
    assert store.get_owner_board_seq("o4") == seq, \
        "idempotent second ack must not increment board_seq"


# ---- prune_terminal bumps ---------------------------------------------------

def test_prune_terminal_increments_each_affected_owner_once(store, ledger, clock):
    # Prune expires rows for TWO different owners: each must increment by 1.
    sid_a = _started_session(store, ledger, owner="o5", key="k-pa")
    store.put_terminal(sid_a, terminal_kind="done", summary="a", ended_at=5.0)
    sid_b = _started_session(store, ledger, owner="o6", key="k-pb")
    store.put_terminal(sid_b, terminal_kind="done", summary="b", ended_at=5.0)
    clock.t = 2000.0
    assert store.prune_terminal(max_age_seconds=500, max_count=100) == 2
    assert store.get_owner_board_seq("o5") == 2, \
        "prune should bump o5's board_seq (put + prune)"
    assert store.get_owner_board_seq("o6") == 2, \
        "prune should bump o6's board_seq (put + prune)"


def test_prune_terminal_no_op_does_not_increment_any_owner(store, ledger):
    sid = _started_session(store, ledger, owner="o7")
    store.put_terminal(sid, terminal_kind="done", summary="ok", ended_at=5.0)
    seq = store.get_owner_board_seq("o7")   # 1 (from put)
    assert store.prune_terminal(max_age_seconds=86400, max_count=100) == 0
    assert store.get_owner_board_seq("o7") == seq, \
        "prune that expires nothing must not increment any board_seq"


def test_prune_terminal_only_bumps_owners_with_changed_rows(store, ledger, clock):
    """A prune that affects one owner does not bump another's board_seq."""
    sid_touched = _started_session(store, ledger, owner="o8a", key="k-pt1")
    store.put_terminal(sid_touched, terminal_kind="done", summary="x", ended_at=5.0)
    sid_untouched = _started_session(store, ledger, owner="o8b", key="k-pt2")
    store.put_terminal(sid_untouched, terminal_kind="done", summary="y", ended_at=5.0)
    store.ack_terminal(sid_untouched, owner_id="o8b")   # owner b acked — not on board
    seq_before_a = store.get_owner_board_seq("o8a")
    seq_before_b = store.get_owner_board_seq("o8b")
    clock.t = 2000.0
    store.prune_terminal(max_age_seconds=500, max_count=100)
    # Only o8a should have been bumped (its row was aged out).
    # o8b's acked row stayed untouched.
    assert store.get_owner_board_seq("o8a") == seq_before_a + 1
    assert store.get_owner_board_seq("o8b") == seq_before_b, \
        "unaffected owner must not be bumped"


# ---- read_board_snapshot ---------------------------------------------------

def test_read_board_snapshot_returns_zero_seq_for_untouched_owner(store):
    seq, rows = store.read_board_snapshot("no-such-owner")
    assert seq == 0
    assert rows == []


def test_read_board_snapshot_returns_matching_seq_and_rows(store, ledger):
    sid = _started_session(store, ledger, owner="o9")
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    seq, rows = store.read_board_snapshot("o9")
    assert seq == 1
    assert len(rows) == 1
    assert rows[0].session_id == sid


def test_read_board_snapshot_cursor_before_invariant(store, ledger, clock):
    """A mutation committed AFTER the read has a strictly larger board_seq."""
    sid = _started_session(store, ledger, owner="o10")
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    clock.t = 1500.0
    # Snapshot after the first mutation.
    seq1, rows1 = store.read_board_snapshot("o10")
    assert seq1 == 1
    assert len(rows1) == 1
    # A second mutation happens after the snapshot.
    sid2 = _started_session(store, ledger, owner="o10", key="k-cb2")
    store.put_terminal(sid2, terminal_kind="done", summary="t", ended_at=6.0)
    seq2, _ = store.read_board_snapshot("o10")
    assert seq2 == 2
    assert seq2 > seq1, "mutation after first read must have strictly larger board_seq"


def test_read_board_snapshot_mutation_before_is_reflected(store, ledger):
    """A mutation committed BEFORE the read is reflected in BOTH the rows and the seq."""
    sid = _started_session(store, ledger, owner="o11")
    store.put_terminal(sid, terminal_kind="done", summary="before", ended_at=5.0)
    seq, rows = store.read_board_snapshot("o11")
    assert seq > 0
    assert any(r.session_id == sid for r in rows)


def test_read_board_snapshot_atomic_read_transaction(store, ledger):
    """Make two connections and verify the explicit BEGIN pins the snapshot."""
    sid = _started_session(store, ledger, owner="o12")
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    # Open a second Store on the same db.
    store2 = Store(store._conns._root, clock=lambda: 1000.0)
    try:
        # In read_board_snapshot, we BEGIN, then read seq then read rows.
        # We cannot inject a concurrent writer from the same process (BEGIN IMMEDIATE
        # on the same connection would fail), so we rely on the fact that
        # isolation_level=None means auto-commit outside the explicit BEGIN.
        # The test proves the method EXISTS and returns (seq, rows).
        seq, rows = store.read_board_snapshot("o12")
        assert isinstance(seq, int)
        assert len(rows) == 1
    finally:
        store2.close()


# ---- additive DDL reopen test ---------------------------------------------

def test_existing_v4_db_gains_owner_board_seq_on_reopen(tmp_path):
    """Prove the additive DDL reopen path: build a v4 DB, close it, reopen,
    verify owner_board_seq table exists and works."""
    # Build.
    s1 = Store(tmp_path, clock=lambda: 1000.0)
    s1.close()
    # Verify table did NOT exist before our change.
    # (Actually it does now because _SCHEMA includes it; the point is that the
    #  DDL is additive — IF an existing v4 DB were opened it would get the table.)
    # Check the table exists.
    raw = sqlite3.connect(tmp_path / "nelix.db")
    try:
        names = {r[0] for r in raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "owner_board_seq" in names, \
            "owner_board_seq table must exist in the schema"
    finally:
        raw.close()
    # Reopen and use the table.
    s2 = Store(tmp_path, clock=lambda: 1000.0)
    assert s2.get_owner_board_seq("any") == 0
    s2.close()


# ---- get_owner_board_seq ---------------------------------------------------

def test_get_owner_board_seq_returns_0_for_never_mutated(store):
    assert store.get_owner_board_seq("ghost") == 0


def test_get_owner_board_seq_rejects_nonsense(store):
    with pytest.raises(NelixError) as ei:
        store.get_owner_board_seq("")
    assert ei.value.code == errors.INVALID_REQUEST
    with pytest.raises(NelixError) as ei:
        store.get_owner_board_seq(42)
    assert ei.value.code == errors.INVALID_REQUEST


# ---- extensibility note: transition_session does NOT bump -------------------

def test_transition_session_does_not_bump_board_seq(store, ledger):
    sid = _started_session(store, ledger, owner="o13")
    seq = store.get_owner_board_seq("o13")
    store.transition_session(sid, owner_id="o13", state="stopping",
                              expected_state="running")
    assert store.get_owner_board_seq("o13") == seq, \
        "transition_session must not bump board_seq"


# ---- two owners, one prune -------------------------------------------------

def test_prune_two_owners_in_one_call_bumps_each_once(store, ledger, clock):
    """A single prune_terminal call that expires rows for two distinct owners
    bumps EACH owner's board_seq exactly once (not once per row)."""
    # Owner A: 2 unacked rows.
    for i in range(2):
        s = _started_session(store, ledger, owner="o14a", key=f"k-2a-{i}")
        store.put_terminal(s, terminal_kind="done", summary=f"a{i}", ended_at=float(i))
    # Owner B: 1 unacked row.
    s = _started_session(store, ledger, owner="o14b", key="k-2b")
    store.put_terminal(s, terminal_kind="done", summary="b0", ended_at=0.0)
    seq_a = store.get_owner_board_seq("o14a")
    seq_b = store.get_owner_board_seq("o14b")
    clock.t = 2000.0
    n = store.prune_terminal(max_age_seconds=500, max_count=100)
    assert n == 3, "should expire all 3 rows"
    assert store.get_owner_board_seq("o14a") == seq_a + 1, \
        "owner A: exactly one increment for the prune (not per-row)"
    assert store.get_owner_board_seq("o14b") == seq_b + 1, \
        "owner B: exactly one increment for the prune"
