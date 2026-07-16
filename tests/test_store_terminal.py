import threading

import pytest

from nelix_contracts import errors
from nelix_contracts.errors import NelixError
from nelix_store.ledger import StartLedger
from nelix_store.store import Store

OID = "o-" + "2" * 32
GID = "g-" + "3" * 32


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
    ledger.assign_generation(r.session_id, GID)
    fields = dict(state="starting", executor="coder", task="t", cwd="/repo",
                  model=None, created_at=100.0)
    fields.update(over)
    store.create_session(r.session_id, **fields)
    return r.session_id


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


def test_prune_removes_acknowledged_records(store, ledger):
    sid = started_session(store, ledger, state="running")
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=100.0)
    store.ack_terminal(sid, owner_id="hermes:local")
    assert store.prune_terminal(max_age_seconds=86400, max_count=100) == 1
    with pytest.raises(NelixError):
        store.get_terminal(sid, owner_id="hermes:local")


def test_prune_reaps_an_abandoned_record_past_max_age(store, ledger):
    sid = started_session(store, ledger, state="running")
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=0.0)
    assert store.prune_terminal(max_age_seconds=500, max_count=100) == 1


def test_prune_bounds_by_count_dropping_oldest_first(store, ledger):
    for i in range(5):
        sid = started_session(store, ledger, key=f"k{i}", state="running")
        store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=float(i))
    assert store.prune_terminal(max_age_seconds=86400, max_count=2) == 3
    assert sorted(r.ended_at for r in store.list_terminal("hermes:local")) == [3.0, 4.0]


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
    with pytest.raises(NelixError):
        store.prune_terminal(**kwargs)


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
        " acknowledged_at, schema_version) VALUES (?,?,?,?,?,?)",
        (other_sid, "done", "s", 200.0, None, 99))
    assert [r.session_id for r in store.list_terminal("hermes:local")] == [sid]
    with pytest.raises(NelixError) as ei:
        store.get_terminal(other_sid, owner_id="hermes:local")
    assert ei.value.code == errors.SCHEMA_TOO_NEW


def test_a_corrupt_terminal_row_does_not_blind_an_owner(store, ledger):
    sid = started_session(store, ledger, key="k1", state="running")
    store.put_terminal(sid, terminal_kind="done", summary="all green", ended_at=100.0)
    other_sid = started_session(store, ledger, key="k2", state="running")
    store._conn.execute(
        "INSERT INTO terminal (session_id, terminal_kind, summary, ended_at,"
        " acknowledged_at, schema_version) VALUES (?,?,?,?,?,?)",
        (other_sid, "done", "s", "soon", None, 1))
    assert [r.session_id for r in store.list_terminal("hermes:local")] == [sid]
