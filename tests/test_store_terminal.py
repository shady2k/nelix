import sqlite3
import threading

import pytest

from nelix_contracts import errors
from nelix_contracts.errors import NelixError
from nelix_contracts.records import SCHEMA_VERSION
from nelix_store.ledger import StartLedger
from nelix_store.store import Store

OID = "o-" + "2" * 32
GID = "g-" + "3" * 32
GID2 = "g-" + "4" * 32
GEPOCH = "g-" + "6" * 32
GEPOCH2 = "g-" + "7" * 32


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


def started_session(store, ledger, owner="hermes:local", key="k1", **over):
    """A session whose identity came from a real start — the only way to make one now."""
    r = ledger.reserve(idempotency_key=key, owner_id=owner, orchestration_id=OID,
                       request_fingerprint="fp")
    ledger.assign_generation(r.session_id, GID, GEPOCH)
    fields = dict(state="starting", executor="coder", task="t", cwd="/repo",
                  model=None, created_at=100.0)
    fields.update(over)
    store.create_session(r.session_id, **fields)
    return r.session_id


def test_republishing_the_same_terminal_result_is_idempotent(store, ledger):
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=5.0)
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=5.0)
    assert store.get_terminal(sid, owner_id="hermes:local").terminal_kind == "done"


@pytest.mark.parametrize("field,value", [
    ("terminal_kind", "error"),
    ("summary", "a different summary"),
    ("ended_at", 99.0),
])
def test_republishing_with_any_canonical_field_changed_is_a_conflict(store, ledger, field, value):
    # One field at a time. The old test changed all three at once, so a mutant comparing only
    # terminal_kind stayed green — the invariant was only a third guarded.
    sid = _live_session(store, ledger)
    base = dict(terminal_kind="done", summary="all green", ended_at=5.0)
    store.put_terminal(sid, **base)
    with pytest.raises(NelixError) as ei:
        store.put_terminal(sid, **{**base, field: value})
    assert ei.value.code == errors.IDEMPOTENCY_CONFLICT
    assert store.get_terminal(sid, owner_id="hermes:local").terminal_kind == "done"


def test_republishing_after_an_ack_neither_conflicts_nor_erases_the_ack(store, ledger):
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=5.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=5.0)
    assert store.get_terminal(sid, owner_id="hermes:local").acknowledged_at == 1000.0
    # Keeping the ack is necessary but not sufficient: the row must also stay OFF the board.
    # The resurrection this all exists to stop needs prune to have run first; this is the same
    # invariant one step earlier, where the payload is still live and a retry could relist it.
    assert store.list_terminal("hermes:local") == [], \
        "a retried put relisted a result the owner had already dismissed"


@pytest.mark.parametrize("field,bad", [
    ("terminal_kind", None), ("terminal_kind", 42), ("summary", 5),
    ("ended_at", "soon"), ("ended_at", float("nan")), ("ended_at", float("inf")),
    ("ended_at", True),
])
def test_put_terminal_rejects_a_malformed_field(store, ledger, field, bad):
    # The TerminalRecord(...) construction inside the transaction had no test at all: no case
    # ever passed a malformed terminal field, so deleting it changed nothing.
    sid = _live_session(store, ledger)
    fields = dict(terminal_kind="done", summary="ok", ended_at=1.0)
    fields[field] = bad
    with pytest.raises(NelixError) as ei:
        store.put_terminal(sid, **fields)
    assert ei.value.code == errors.INVALID_REQUEST


def _live_session(store, ledger, owner="hermes:local", key="k1", **over):
    return started_session(store, ledger, owner=owner, key=key, state="running", **over)


def _acked_setup(tmp_path, clock):
    """A published, UNacknowledged terminal record on a Store whose clock is `clock` by the
    time the ack runs.

    The clock is SWAPPED IN after publication rather than passed to the constructor. It used to
    be a constructor argument, on the premise that "a nonsense clock cannot disturb the setup,
    because this Store's clock is read only by ack_terminal and prune_terminal". published_at
    ended that: put_terminal reads the clock too, so a nonsense one now rejects the PUT and the
    test would never reach the ack it exists to probe — it would pass for the wrong reason, on
    the setup instead of the subject. Swapping after publication keeps the probe isolated to
    ack's own read, which is what these tests are about.
    """
    lg = StartLedger(tmp_path, clock=lambda: 1000.0)
    try:
        store = Store(tmp_path, clock=lambda: 1000.0)
        sid = _live_session(store, lg)
        store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
        store._clock = clock
    finally:
        lg.close()
    return store, sid


def test_a_terminal_record_cannot_exist_without_its_session(store):
    with pytest.raises(NelixError) as ei:
        store.put_terminal("s-" + "9" * 32, terminal_kind="done", summary="s", ended_at=1.0)
    assert ei.value.code == errors.UNKNOWN_SESSION


def test_a_terminal_record_inherits_its_owner_from_its_session(store, ledger):
    # THE Critical: rev 3 accepted owner_id on put_terminal, so a terminal record could be
    # filed under a DIFFERENT owner than its session — and that owner's board would then
    # show someone else's result. The parameter is gone; there is nothing to disagree with.
    #
    # TWO owners, each with their own session + terminal record: a single owner cannot catch
    # a join hardcoded to a constant owner (e.g. `_TERMINAL_SELECT` joining `starts` on
    # `st.owner_id = 'hermes:local'` instead of `st.session_id = t.session_id`) — with only
    # one owner in the store, that literal is indistinguishable from a correct join. A second
    # owner makes the mismatch observable: rev4's f1k-rev4-report.md documents exactly this
    # gap (mutation 4) and a reproduction with a second owner ("claude-code:1") showing the
    # second owner's result coming back mislabeled as the first owner's.
    sid_a = started_session(store, ledger, owner="hermes:local", key="k-a")
    store.put_terminal(sid_a, terminal_kind="done", summary="A's result", ended_at=5.0)

    sid_b = started_session(store, ledger, owner="claude-code:1", key="k-b")
    store.put_terminal(sid_b, terminal_kind="done", summary="B's result", ended_at=6.0)

    rec_a = store.get_terminal(sid_a, owner_id="hermes:local")
    assert (rec_a.owner_id, rec_a.session_id, rec_a.summary) == (
        "hermes:local", sid_a, "A's result")

    rec_b = store.get_terminal(sid_b, owner_id="claude-code:1")
    assert (rec_b.owner_id, rec_b.session_id, rec_b.summary) == (
        "claude-code:1", sid_b, "B's result")

    assert [r.session_id for r in store.list_terminal("hermes:local")] == [sid_a]
    assert [r.session_id for r in store.list_terminal("claude-code:1")] == [sid_b]


def test_an_unacknowledged_result_survives_far_past_the_old_300s_ttl(store, ledger, clock):
    # The defect this package exists to kill: the live daemon expires terminal snapshots
    # after terminal_snapshot_ttl=300.0, so a harness away six minutes lost the result.
    sid = started_session(store, ledger, state="running")
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=1000.0)
    clock.t = 1000.0 + 3600
    assert store.prune_terminal(max_age_seconds=86400, max_count=100) == 0
    assert store.get_terminal(sid, owner_id="hermes:local").terminal_kind == "done"


def test_ack_is_idempotent(store, ledger, clock):
    sid = started_session(store, ledger, state="running")
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=100.0)
    first = store.ack_terminal(sid, owner_id="hermes:local")
    clock.t = 2000.0
    second = store.ack_terminal(sid, owner_id="hermes:local")
    assert first.acknowledged_at == 1000.0
    assert second.acknowledged_at == 1000.0


def test_a_retried_put_never_erases_an_acknowledgement(store, ledger):
    # The generation may re-publish a terminal record after the owner already acked it.
    # rev 1's unconditional write reset acknowledged_at to None.
    sid = started_session(store, ledger, state="running")
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=100.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=100.0)
    assert store.get_terminal(sid, owner_id="hermes:local").acknowledged_at == 1000.0


def test_concurrent_acks_agree_on_one_timestamp(tmp_path):
    # rev 1's ack was a read-modify-write: two callers both saw None, both stamped, and the
    # later write won — so "the original timestamp never changes" was false under the only
    # conditions that matter. Sequential tests cannot see this.
    ticks = iter(range(1, 10_000))
    # This first Store() also bootstraps the database, so the threads below race ONLY the
    # ack CAS — not concurrent first-open.
    store = Store(tmp_path, clock=lambda: float(next(ticks)))
    ledger = StartLedger(tmp_path, clock=lambda: 1000.0)
    sid = started_session(store, ledger, state="running")
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=100.0)
    ledger.close()
    store.close()

    results, barrier = [], threading.Barrier(8)

    def ack():
        s = Store(tmp_path, clock=lambda: float(next(ticks)))
        barrier.wait()
        try:
            results.append(s.ack_terminal(sid, owner_id="hermes:local").acknowledged_at)
        finally:
            s.close()

    threads = [threading.Thread(target=ack) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "a thread hung"
    assert len(results) == 8
    assert len(set(results)) == 1, f"acks disagreed on the timestamp: {sorted(set(results))}"


def test_acknowledging_removes_a_result_from_the_board_at_once(store, ledger):
    # list_terminal filtered on owner and schema_version and NOTHING else — there was no
    # acknowledged filter at all. So an acked result stayed on the owner's board until the
    # pruner happened to run, which made "acknowledge" mean "dismiss, eventually, on the GC's
    # schedule". Logical dismissal (ack) and physical reclamation (prune) are different
    # events, and only the first is the owner's to decide.
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    assert [r.session_id for r in store.list_terminal("hermes:local")] == [sid]
    store.ack_terminal(sid, owner_id="hermes:local")
    assert store.list_terminal("hermes:local") == [], \
        "an acknowledged result was still on the board; only prune removed it"


def test_acknowledging_does_not_hide_a_result_from_a_direct_get(store, ledger):
    # The filter belongs on the BOARD, not on the shared _TERMINAL_SELECT. A get by id is not
    # the board: ack_terminal re-reads through get_terminal inside its own transaction, and
    # ack's idempotency (a repeated ack returns the ORIGINAL timestamp) is exactly the ability
    # to read an acknowledged row back by id. This test passes both before and after the
    # board filter; its job is to fail on the WRONG fix, and it does.
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    assert store.get_terminal(sid, owner_id="hermes:local").acknowledged_at == 1000.0
    assert store.ack_terminal(sid, owner_id="hermes:local").acknowledged_at == 1000.0


def test_an_acknowledged_result_cannot_be_resurrected_by_a_retried_put(store, ledger):
    # THE invariant this whole task exists for. Terminal idempotency has no key column: its
    # effective key is session_id and its remembered outcome is (terminal_kind, summary,
    # ended_at) IN THE ROW PRUNE DELETES. Neither starts nor sessions remembers the result or
    # the ack, so once prune reclaims the row nothing survives to recognise the retry — the
    # retry inserts a fresh UNACKNOWLEDGED row and the dismissed result is back on the board.
    #
    # The window is not narrow. prune's condition is `acknowledged_at IS NOT NULL OR ...`, so
    # max_age_seconds gates only UNACKNOWLEDGED rows: an acked row is eligible on the very next
    # prune at any age. The ack->prune window is ZERO, so an ordinary sub-second retry lands in
    # it, and no "minimum retention" would fix that.
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=5.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    store.prune_terminal(max_age_seconds=86400, max_count=100)
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=5.0)
    assert store.list_terminal("hermes:local") == [], \
        "the owner dismissed this result and a retried put put it back on their board"
    assert store.get_terminal(sid, owner_id="hermes:local").acknowledged_at == 1000.0, \
        "the retry erased the acknowledgement it should have recognised"


def test_pruning_an_acknowledged_result_keeps_its_receipt(store, ledger):
    # REPLACES test_prune_removes_acknowledged_records, which asserted THE DEFECT: it acked,
    # pruned, and demanded the row be gone and get_terminal raise UNKNOWN_SESSION. Deleting the
    # row destroys the only evidence that this session ever ended, and terminal idempotency is
    # keyed on exactly that evidence — so the next matching put re-publishes the dismissed
    # result. prune's condition was `acknowledged_at IS NOT NULL OR (age)`, meaning max_age
    # gated only UNACKNOWLEDGED rows and an acked row was eligible on the very next prune at any
    # age: the ack->prune window is ZERO, not hours.
    #
    # The receipt is now permanent. Reclamation is not prune's to do: a receipt lives at least
    # as long as its session and its start, and nothing GCs those either.
    sid = started_session(store, ledger, state="running")
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=100.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    assert store.prune_terminal(max_age_seconds=86400, max_count=100) == 0, \
        "prune reclaimed an acknowledged receipt; the next matching put would resurrect it"
    rec = store.get_terminal(sid, owner_id="hermes:local")
    assert rec.acknowledged_at == 1000.0
    assert store.list_terminal("hermes:local") == []      # dismissed, and it stays dismissed


def test_an_abandoned_result_expires_into_a_receipt_rather_than_vanishing(store, ledger, clock):
    # Age-pruning an UNACKNOWLEDGED result used to delete it outright, so afterwards the store
    # could not tell "this session never ended" from "it ended and you were too late" — both
    # answered UNKNOWN_SESSION. The payload was the receipt, so losing one lost the other.
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    clock.t = 1600.0
    assert store.prune_terminal(max_age_seconds=500, max_count=100) == 1
    assert store.list_terminal("hermes:local") == []
    rec = store.get_terminal(sid, owner_id="hermes:local")
    assert (rec.expired_at, rec.expire_reason) == (1600.0, "age")
    assert rec.acknowledged_at is None       # nobody ever dismissed it; it timed out


def test_count_pruning_expires_into_receipts(store, ledger, clock):
    sid_old = None
    for i in range(3):
        clock.t = 1000.0 + i
        sid = started_session(store, ledger, key=f"k{i}", state="running")
        store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=float(i))
        if i == 0:
            sid_old = sid
    assert store.prune_terminal(max_age_seconds=86400, max_count=1) == 2
    rec = store.get_terminal(sid_old, owner_id="hermes:local")
    assert (rec.expired_at, rec.expire_reason) == (1002.0, "count")


def test_acknowledging_an_expired_result_says_so_instead_of_denying_it_existed(store, ledger,
                                                                               clock):
    # THE matrix row "expired | ack | terminal_expired". Deleting the row made this
    # UNKNOWN_SESSION — the same answer as a session id that was never real. An owner who acks a
    # result the pruner retired a moment earlier deserves to be told which of those happened.
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    clock.t = 1600.0
    store.prune_terminal(max_age_seconds=500, max_count=100)
    with pytest.raises(NelixError) as ei:
        store.ack_terminal(sid, owner_id="hermes:local")
    assert ei.value.code == errors.TERMINAL_EXPIRED
    assert ei.value.retryable is False       # no retry of the same ack un-expires it


def test_terminal_expired_is_reported_for_a_prune_that_committed_first(store, ledger, clock):
    # The matrix's "prune committed first | ack | DETERMINISTIC terminal_expired". The seam test
    # covers the prune that loses the race; this is the one that wins it, and the point is that
    # the loser is now a named outcome rather than a vanished row.
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    clock.t = 1600.0
    assert store.prune_terminal(max_age_seconds=500, max_count=100) == 1
    for _ in range(3):
        with pytest.raises(NelixError) as ei:
            store.ack_terminal(sid, owner_id="hermes:local")
        assert ei.value.code == errors.TERMINAL_EXPIRED


def test_republishing_an_expired_result_does_not_resurrect_it(store, ledger, clock):
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    clock.t = 1600.0
    store.prune_terminal(max_age_seconds=500, max_count=100)
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)   # same result
    assert store.list_terminal("hermes:local") == [], \
        "a retry put an EXPIRED result back on the board"
    rec = store.get_terminal(sid, owner_id="hermes:local")
    assert (rec.expired_at, rec.expire_reason) == (1600.0, "age")


def test_republishing_an_expired_result_differently_is_still_a_conflict(store, ledger, clock):
    # Expiry reclaims the BOARD SLOT, not the outcome. The first valid outcome is immutable for
    # this session id whether it is live, dismissed or expired — otherwise expiry would quietly
    # become a way to overwrite history by waiting.
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    clock.t = 1600.0
    store.prune_terminal(max_age_seconds=500, max_count=100)
    with pytest.raises(NelixError) as ei:
        store.put_terminal(sid, terminal_kind="error", summary="s", ended_at=5.0)
    assert ei.value.code == errors.IDEMPOTENCY_CONFLICT


def test_republishing_an_acknowledged_result_differently_is_a_conflict(store, ledger):
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    with pytest.raises(NelixError) as ei:
        store.put_terminal(sid, terminal_kind="error", summary="s", ended_at=5.0)
    assert ei.value.code == errors.IDEMPOTENCY_CONFLICT


def test_a_repeated_ack_after_a_prune_returns_the_original_acknowledgement(store, ledger, clock):
    # The matrix's "ack-compacted | repeated ack | the ORIGINAL acknowledgement". Under the old
    # code prune deleted the row, so this raised UNKNOWN_SESSION: ack's idempotency guarantee
    # ("a repeated ack returns the same timestamp") silently expired whenever the GC ran.
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    clock.t = 9000.0
    store.prune_terminal(max_age_seconds=500, max_count=100)
    assert store.ack_terminal(sid, owner_id="hermes:local").acknowledged_at == 1000.0


def test_a_stale_worker_clock_cannot_reap_a_fresh_result(store, ledger, clock):
    # ended_at is CALLER-supplied and prune aged against it, so a worker's own clock decided how
    # long its own result was retained. A worker whose clock is stale (or that reports a
    # monotonic/uptime value, or 0.0) has its result reaped on the first prune — the owner never
    # sees it. Retention is a STORE policy.
    #
    # max_age MUST fall between the two ages or the test cannot discriminate: the store published
    # 1s ago, the worker claims it ended 1001s ago. The plan's 86400 is above BOTH (1001 is not
    # > 86400), which is why the plan's version of this test passes on the unfixed code.
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=0.0)
    clock.t = 1001.0
    assert store.prune_terminal(max_age_seconds=500, max_count=100) == 0, \
        "a stale ended_at reaped a result the store published one second ago"
    assert store.get_terminal(sid, owner_id="hermes:local").ended_at == 0.0, \
        "ended_at is the worker's reported fact and must survive verbatim"


def test_a_future_worker_clock_cannot_make_a_result_immortal(store, ledger, clock):
    # The other half, and the worse one: a worker with a skewed-FORWARD clock reported an
    # ended_at in the future, so (now - ended_at) stayed negative and the row outlived every
    # age bound — unbounded growth chosen by the least trustworthy party in the system.
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=1e9)
    clock.t = 2000.0
    assert store.prune_terminal(max_age_seconds=500, max_count=100) == 1, \
        "a future ended_at made the result immortal; the store published it 1000s ago"


def test_a_matching_retry_does_not_extend_retention(store, ledger, clock):
    # NOT a red test for the old defect — it passes on the unfixed code, because today ended_at
    # simply never changes on a retry, so there is nothing to extend. It guards the NEW column's
    # semantics: published_at is stamped on FIRST publication only. Were put_terminal to restamp
    # it on every matching retry, a generation retrying in a loop would keep a result alive
    # forever and defeat the age bound it just moved to.
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    clock.t = 5000.0
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    assert store.prune_terminal(max_age_seconds=100, max_count=100) == 1, \
        "a retry refreshed published_at, so a result could be kept alive forever by retrying"


def test_prune_reaps_an_abandoned_record_past_max_age(store, ledger, clock):
    # MEANING CHANGED, not just the column. "Past max_age" used to mean "the WORKER says it
    # ended long ago", which this test got for free from ended_at=0.0 against a clock at 1000 —
    # the store had published the row an instant earlier. It now means "the STORE published it
    # long ago", so the test must let the store's own time actually pass. That is the defect
    # this column exists for, seen from the test's side: the old assertion could be satisfied by
    # a worker lying about its clock, without a second of real retention elapsing.
    sid = started_session(store, ledger, state="running")
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=0.0)
    clock.t = 1600.0                    # published at 1000: 600s of real retention
    assert store.prune_terminal(max_age_seconds=500, max_count=100) == 1


def test_prune_bounds_by_count_dropping_oldest_first(store, ledger, clock):
    # MEANING CHANGED: "oldest" is now "published earliest" (the store's fact), not "the worker
    # says it ended earliest".
    #
    # ended_at DESCENDS as published_at ASCENDS, deliberately. The obvious update to this test
    # — advance the clock per publication and leave ended_at ascending too — leaves both columns
    # in the SAME order, so ordering by either keeps the same rows and the test cannot see which
    # column the query used. MEASURED: written that way, reverting the ORDER BY to ended_at kept
    # this test green (0/1). Only disagreeing orders probe the guard.
    published = []
    for i in range(5):
        clock.t = 1000.0 + i
        sid = started_session(store, ledger, key=f"k{i}", state="running")
        store.put_terminal(sid, terminal_kind="done", summary="all green",
                           ended_at=float(100 - i))
        published.append(sid)
    assert store.prune_terminal(max_age_seconds=86400, max_count=2) == 3
    assert sorted(r.session_id for r in store.list_terminal("hermes:local")) == \
        sorted(published[3:]), "count-pruning kept the rows the WORKERS called newest"


def test_a_noisy_owner_cannot_evict_a_quiet_owners_unacked_result(store, ledger):
    # THE rev 1 Critical, probe-proven by review: the count bound was applied across ALL
    # owners, so one owner's churn deleted another's unacknowledged result — violating both
    # "unacked results survive" and "owner is a correctness namespace".
    quiet_sid = started_session(store, ledger, owner="quiet:1", key="k-quiet", state="running")
    store.put_terminal(quiet_sid, terminal_kind="done", summary="all green", ended_at=1.0)
    for i in range(5):
        sid = started_session(store, ledger, owner="noisy:1", key=f"k-noisy-{i}",
                              state="running")
        store.put_terminal(sid, terminal_kind="done", summary="all green",
                           ended_at=float(100 + i))
    store.prune_terminal(max_age_seconds=86400, max_count=3)
    assert store.get_terminal(quiet_sid, owner_id="quiet:1").ended_at == 1.0
    assert len(store.list_terminal("noisy:1")) == 3


def test_prune_ties_break_deterministically(store, ledger):
    # Tie-break is ORDER BY ended_at DESC, session_id DESC — session ids are now minted
    # uuid4s (nelix_contracts.ids.new_session_id), not the fixed lexicographic literals rev 3
    # used, so the survivor must be computed the same way SQL picks it: the lexicographically
    # greatest id, not "whichever we created last".
    sids = []
    for i in range(3):
        sid = started_session(store, ledger, key=f"k{i}", state="running")
        store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=5.0)
        sids.append(sid)
    store.prune_terminal(max_age_seconds=86400, max_count=1)
    assert [r.session_id for r in store.list_terminal("hermes:local")] == [max(sids)]


@pytest.mark.parametrize("kwargs", [{"max_age_seconds": -1, "max_count": 1},
                                    {"max_age_seconds": 1, "max_count": -1}])
def test_prune_rejects_nonsense_bounds(store, kwargs):
    with pytest.raises(NelixError) as ei:
        store.prune_terminal(**kwargs)
    assert ei.value.code == errors.INVALID_REQUEST


def test_terminal_reads_are_owner_filtered(store, ledger):
    sid = started_session(store, ledger, state="running")
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=100.0)
    with pytest.raises(NelixError) as ei:
        store.get_terminal(sid, owner_id="claude-code:1")
    assert ei.value.code == errors.OWNER_MISMATCH
    assert store.list_terminal("claude-code:1") == []


def test_one_future_schema_row_does_not_brick_an_owners_terminal_board(store, ledger):
    # The untested half of rev 2's Critical fix: list_sessions was covered, list_terminal
    # was not.
    sid = started_session(store, ledger, key="k1", state="running")
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=100.0)
    other_sid = started_session(store, ledger, key="k2", state="running")
    store._conn.execute(
        "INSERT INTO terminal (session_id, terminal_kind, summary, ended_at,"
        " published_at, acknowledged_at, schema_version) VALUES (?,?,?,?,?,?,?)",
        (other_sid, "done", "s", 200.0, 1000.0, None, SCHEMA_VERSION + 1))
    assert [r.session_id for r in store.list_terminal("hermes:local")] == [sid]
    with pytest.raises(NelixError) as ei:
        store.get_terminal(other_sid, owner_id="hermes:local")
    assert ei.value.code == errors.SCHEMA_TOO_NEW


def test_a_corrupt_terminal_row_does_not_blind_an_owner(store, ledger):
    # schema_version is SCHEMA_VERSION, never the literal 1 it used to be. This test proves that
    # _read_rows SKIPS a row it cannot decode (here: ended_at="soon", legal affinity, illegal
    # value) — but list_terminal also filters `t.schema_version=?` in SQL, so the moment the
    # constant moved past 1 the hardcoded row was excluded by the VERSION filter and never
    # reached the decoder at all. The test would have stayed green while proving nothing. Pinned
    # to the constant so it keeps testing the skip rather than the filter.
    sid = started_session(store, ledger, key="k1", state="running")
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=100.0)
    other_sid = started_session(store, ledger, key="k2", state="running")
    store._conn.execute(
        "INSERT INTO terminal (session_id, terminal_kind, summary, ended_at,"
        " published_at, acknowledged_at, schema_version) VALUES (?,?,?,?,?,?,?)",
        (other_sid, "done", "s", "soon", 1000.0, None, SCHEMA_VERSION))
    assert [r.session_id for r in store.list_terminal("hermes:local")] == [sid]


def _raw_terminal(store, sid, **over):
    """Write a terminal row straight past put_terminal, to probe the CHECK constraints.

    The constraints exist for durable states this package's own writers cannot produce — a
    future writer, a repair script, a half-applied migration. So the probe has to bypass the
    writer too, or it would only be testing put_terminal.
    """
    row = dict(session_id=sid, terminal_kind="done", summary="s", ended_at=5.0,
               published_at=1000.0, acknowledged_at=None, expired_at=None, expire_reason=None,
               schema_version=SCHEMA_VERSION)
    row.update(over)
    cols = ", ".join(row)
    store._conn.execute(f"INSERT INTO terminal ({cols}) VALUES ({', '.join('?' * len(row))})",
                        tuple(row.values()))


@pytest.mark.parametrize("state,why", [
    (dict(expired_at=1600.0, expire_reason=None), "expired with no reason"),
    (dict(expired_at=None, expire_reason="age"), "a reason but not expired"),
    (dict(expired_at=1600.0, expire_reason="whenever"), "an unnameable reason"),
    (dict(expired_at=1600.0, expire_reason="acknowledged"), "dismissal masquerading as expiry"),
    (dict(expired_at=1600.0, expire_reason="age", acknowledged_at=1000.0),
     "expired AND acknowledged"),
])
def test_an_impossible_terminal_state_cannot_be_stored(store, ledger, state, why):
    # Each of these is a durable state that would make the lifecycle unreadable: a row that is
    # expired for no reason, or dismissed and timed-out at once, cannot be answered coherently
    # by ack or by the board. Unrepresentable beats untested — a CHECK makes SQLite refuse it at
    # the write, so no reader has to carry a branch for a state that cannot arrive.
    sid = _live_session(store, ledger)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _raw_terminal(store, sid, **state)


def test_a_receipt_cannot_be_erased_by_deleting_its_session(store, ledger):
    # ON DELETE RESTRICT, never CASCADE. The receipt is what makes an acknowledged result
    # unresurrectable; a cascade from sessions would delete it silently and hand the
    # resurrection bug straight back through a different door.
    sid = _live_session(store, ledger)
    store.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        store._conn.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
    assert store.get_terminal(sid, owner_id="hermes:local").terminal_kind == "done"


def test_a_session_cannot_be_erased_by_deleting_its_start(store, ledger):
    sid = _live_session(store, ledger)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        store._conn.execute("DELETE FROM starts WHERE session_id=?", (sid,))


def test_receipts_do_not_consume_the_owners_board_budget(store, ledger, clock):
    # The count bound bounds THE BOARD, so it must count board rows. Were receipts counted, an
    # owner who diligently acks their results would watch those dead receipts evict the live
    # ones they have not read yet — the bound would punish exactly the behaviour it exists to
    # support, and the eviction would look like data loss.
    for i in range(4):
        clock.t = 1000.0 + i
        acked = started_session(store, ledger, key=f"done{i}", state="running")
        store.put_terminal(acked, terminal_kind="done", summary="s", ended_at=float(i))
        store.ack_terminal(acked, owner_id="hermes:local")
    clock.t = 2000.0
    live = _live_session(store, ledger, key="live")
    store.put_terminal(live, terminal_kind="done", summary="s", ended_at=9.0)
    assert store.prune_terminal(max_age_seconds=86400, max_count=2) == 0
    assert [r.session_id for r in store.list_terminal("hermes:local")] == [live]


def test_a_prune_cannot_land_between_the_ack_and_its_reread(tmp_path):
    # ack_terminal is one transaction: read -> CAS -> re-read. Without it, a prune landing
    # between the CAS and the re-read makes an ack that DURABLY SUCCEEDED report
    # unknown_session — a non-retryable error for an operation that worked.
    #
    # THE SEAM, and both halves are load-bearing:
    #  * SYNCHRONOUS, on this thread. The pruner Store is built here, and db.connect() does not
    #    set check_same_thread=False, so pruning from a spawned thread raises before reaching
    #    ANY SQLite locking — it would race nothing. (Nor can the Store be built inside the
    #    thread: connect() runs executescript(_SCHEMA), which needs a write lock the acker
    #    holds, so it would fail at connect rather than at prune.)
    #  * BEFORE the re-read, not after. The prune must land while ack is between its CAS and
    #    its re-read. The previous spy did `record = real_get(...)` and only THEN counted and
    #    pruned, so on the second call the re-read had already returned and the prune raced
    #    nothing. Measured: with both bugs present, deleting BEGIN IMMEDIATE caught 0/5.
    from nelix_store.ledger import StartLedger
    from nelix_store.store import Store

    lg = StartLedger(tmp_path, clock=lambda: 1000.0)
    acker = Store(tmp_path, clock=lambda: 1000.0)
    try:
        sid = _live_session(acker, lg)
        acker.put_terminal(sid, terminal_kind="done", summary="s", ended_at=5.0)

        # busy_timeout=0: the pruner must FAIL FAST when ack holds the write lock rather than
        # wait for it — otherwise "blocked" and "succeeded" look identical to the test.
        pruner = Store(tmp_path, clock=lambda: 1000.0, timeout=0.0)
        calls, pruned = [], []
        real_get = acker.get_terminal

        def spy(session_id, *, owner_id):
            calls.append(session_id)
            if len(calls) == 2:                  # between the CAS and the re-read
                pruned.append(_try_prune(pruner))
            return real_get(session_id, owner_id=owner_id)

        acker.get_terminal = spy
        record = acker.ack_terminal(sid, owner_id="hermes:local")   # must NOT raise
        assert record.acknowledged_at == 1000.0
        # The ack surviving is necessary but NOT sufficient: a prune that died of an unrelated
        # error would leave the ack intact too and look identical. Assert the prune was
        # BLOCKED BY THE WRITE LOCK — that is the thing the transaction actually does. This
        # assertion is what would have caught the wrong-thread bug that hid this seam for
        # seven revisions: it returned store_corrupt, not store_unavailable.
        assert pruned == [errors.STORE_UNAVAILABLE], f"the prune did not race the ack: {pruned}"
        pruner.close()
    finally:
        lg.close()
        acker.close()


def _try_prune(store):
    try:
        return store.prune_terminal(max_age_seconds=0, max_count=100)
    except NelixError as e:
        return e.code


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf")])
def test_prune_rejects_a_nonsense_max_age(store, bad):
    # NaN is the sharp one: every (now - ended_at) > NaN comparison is False, so age pruning
    # SILENTLY stops working — the bound looks configured and does nothing.
    with pytest.raises(NelixError) as ei:
        store.prune_terminal(max_age_seconds=bad, max_count=1)
    assert ei.value.code == errors.INVALID_REQUEST


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True,
                                 "not-a-number", None])
def test_prune_rejects_a_nonsense_clock(tmp_path, bad):
    # The SAME defect as the max_age guard above, re-entering through the injected clock:
    # validating the parameter and then reading `now` from an unchecked clock leaves the
    # identical hole. NaN reaps NOTHING (every comparison False) while the bound looks
    # configured; +inf is worse and reaps EVERY record including unacknowledged ones; a
    # non-numeric clock escaped store.py as a raw ValueError/TypeError, not a contract error.
    store = Store(tmp_path, clock=lambda: bad)
    try:
        with pytest.raises(NelixError) as ei:
            store.prune_terminal(max_age_seconds=60, max_count=100)
        assert ei.value.code == errors.INVALID_REQUEST
    finally:
        store.close()


def test_a_nonsense_clock_cannot_make_ack_a_silent_no_op(tmp_path):
    # The SHARPEST case, and the mechanism is not the obvious one — measured, not reasoned:
    # SQLite silently COERCES NaN to NULL on write (nan -> stored NULL; inf and -inf store
    # as-is). So the CAS `SET acknowledged_at=NaN ... AND acknowledged_at IS NULL` stamps
    # NULL; its own guard therefore still matches next time; the re-read returns
    # acknowledged_at=None, which is VALID because the field is optional; nothing raises, so
    # the transaction COMMITS. Measured end to end on 3d25a1b: ack_terminal RETURNED SUCCESS
    # with acknowledged_at=None and the row left unacknowledged. Silently, and forever.
    #
    # 67c300c guarded prune_terminal's clock read and left this one, so the package rejected a
    # nonsense clock in one method and lied about the outcome in another.
    store, sid = _acked_setup(tmp_path, clock=lambda: float("nan"))
    try:
        with pytest.raises(NelixError) as ei:
            store.ack_terminal(sid, owner_id="hermes:local")
        assert ei.value.code == errors.INVALID_REQUEST
        row = store._conn.execute(
            "SELECT acknowledged_at FROM terminal WHERE session_id=?", (sid,)).fetchone()
        assert row["acknowledged_at"] is None, "a rejected ack must not have written anything"
    finally:
        store.close()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True,
                                 "not-a-number", None])
def test_ack_rejects_a_nonsense_clock(tmp_path, bad):
    # Measured on 3d25a1b, and the outcomes DIFFER per value — which is exactly why the guard
    # belongs on the read rather than on any one symptom:
    #   nan   -> success, nothing acknowledged (above)
    #   inf   -> stored as inf, re-read's TerminalRecord rejects it, `with self._conn:` rolls
    #            back, row left clean: ack raised STORE_CORRUPT — naming OUR construction
    #            argument as the caller's data rotting
    #   True  -> silently 1.0, a real acknowledgement at a fictional time (bool is an int
    #            subclass, which is why timestamp() rejects it explicitly)
    #   "..."/None -> raw ValueError/TypeError, straight out through the package boundary:
    #            translates_sqlite only catches sqlite3.Error
    # One finite clock read closes all four. invalid_request names the right party: the clock
    # is this Store's own construction argument and no retry of the same call can fix it.
    store, sid = _acked_setup(tmp_path, clock=lambda: bad)
    try:
        with pytest.raises(NelixError) as ei:
            store.ack_terminal(sid, owner_id="hermes:local")
        assert ei.value.code == errors.INVALID_REQUEST
    finally:
        store.close()


# ---- per-generation terminal_seq watermarks (nelix-gm3) ----

def test_put_terminal_assigns_monotonic_terminal_seq_per_generation(store, ledger, clock):
    """After N terminals persist on a generation, terminal_seq values are monotonic,
    gap-free-enough ordinals per generation. Revert the seq assignment -> this test fails."""
    r_b = ledger.reserve(idempotency_key="k-seq-b", owner_id="hermes:local",
                          orchestration_id=OID, request_fingerprint="fp")
    ledger.assign_generation(r_b.session_id, GID2, GEPOCH2)
    store.create_session(r_b.session_id, state="running", executor="coder", task="t",
                          cwd="/repo", model=None, created_at=100.0)

    seqs_a = []
    for i in range(3):
        clock.t = 1000.0 + i
        sid = started_session(store, ledger, key=f"k-seq-a{i}", state="running")
        store.put_terminal(sid, terminal_kind="done", summary=f"seq test {i}",
                           ended_at=float(i))
        rec = store.get_terminal(sid, owner_id="hermes:local")
        seqs_a.append(rec.terminal_seq)

    clock.t = 1000.0
    r_b2 = ledger.reserve(idempotency_key="k-seq-b2", owner_id="hermes:local",
                           orchestration_id=OID, request_fingerprint="fp")
    ledger.assign_generation(r_b2.session_id, GID2, GEPOCH2)
    store.create_session(r_b2.session_id, state="running", executor="coder", task="t",
                          cwd="/repo", model=None, created_at=100.0)
    sid_b2 = r_b2.session_id
    store.put_terminal(sid_b2, terminal_kind="done", summary="gen2 first", ended_at=0.0)
    r_c = ledger.reserve(idempotency_key="k-seq-b3", owner_id="hermes:local",
                          orchestration_id=OID, request_fingerprint="fp")
    ledger.assign_generation(r_c.session_id, GID2, GEPOCH2)
    store.create_session(r_c.session_id, state="running", executor="coder", task="t",
                          cwd="/repo", model=None, created_at=100.0)
    sid_b3 = r_c.session_id
    store.put_terminal(sid_b3, terminal_kind="done", summary="gen2 second", ended_at=1.0)

    assert seqs_a == [1, 2, 3], f"expected [1, 2, 3], got {seqs_a}"
    rec_b = store.get_terminal(sid_b2, owner_id="hermes:local")
    assert rec_b.terminal_seq == 1, f"expected seq=1 for first terminal on gen2, got {rec_b.terminal_seq}"
    rec_c = store.get_terminal(sid_b3, owner_id="hermes:local")
    assert rec_c.terminal_seq == 2, f"expected seq=2 for second terminal on gen2, got {rec_c.terminal_seq}"


def test_get_generation_persisted_high_water(store, ledger, clock):
    """Verify get_generation_persisted_high_water returns the correct max per epoch."""
    # No terminals yet -> 0
    assert store.get_generation_persisted_high_water(GEPOCH) == 0

    # One terminal -> seq 1
    sid1 = started_session(store, ledger, key="k-hw-1", state="running")
    store.put_terminal(sid1, terminal_kind="done", summary="first", ended_at=1.0)
    assert store.get_generation_persisted_high_water(GEPOCH) == 1

    # Another terminal -> seq 2
    sid2 = started_session(store, ledger, key="k-hw-2", state="running")
    store.put_terminal(sid2, terminal_kind="done", summary="second", ended_at=2.0)
    assert store.get_generation_persisted_high_water(GEPOCH) == 2

    # Second epoch separate counter
    r = ledger.reserve(idempotency_key="k-hw-gen2", owner_id="hermes:local",
                        orchestration_id=OID, request_fingerprint="fp")
    ledger.assign_generation(r.session_id, GID2, GEPOCH2)
    store.create_session(r.session_id, state="running", executor="coder", task="t",
                          cwd="/repo", model=None, created_at=100.0)
    store.put_terminal(r.session_id, terminal_kind="done", summary="gen2 only", ended_at=3.0)
    assert store.get_generation_persisted_high_water(GEPOCH2) == 1
    assert store.get_generation_persisted_high_water(GEPOCH) == 2  # unchanged
