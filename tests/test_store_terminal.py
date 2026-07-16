import threading

import pytest

from nelix_contracts import errors
from nelix_contracts.errors import NelixError
from nelix_contracts.records import TerminalRecord
from nelix_store.store import Store

OID = "o-" + "2" * 32
GID = "g-" + "3" * 32


def make_terminal(sid, owner="hermes:local", ended_at=100.0, **over):
    fields = dict(session_id=sid, owner_id=owner, orchestration_id=OID, generation_id=GID,
                  terminal_kind="done", summary="all green", ended_at=ended_at)
    fields.update(over)
    return TerminalRecord(**fields)


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


def test_an_unacknowledged_result_survives_far_past_the_old_300s_ttl(store, clock):
    # The defect this package exists to kill: the live daemon expires terminal snapshots
    # after terminal_snapshot_ttl=300.0, so a harness away six minutes lost the result.
    sid = "s-" + "1" * 32
    store.put_terminal(make_terminal(sid, ended_at=1000.0))
    clock.t = 1000.0 + 3600
    assert store.prune_terminal(max_age_seconds=86400, max_count=100) == 0
    assert store.get_terminal(sid, owner_id="hermes:local").terminal_kind == "done"


def test_ack_is_idempotent(store, clock):
    sid = "s-" + "1" * 32
    store.put_terminal(make_terminal(sid))
    first = store.ack_terminal(sid, owner_id="hermes:local")
    clock.t = 2000.0
    second = store.ack_terminal(sid, owner_id="hermes:local")
    assert first.acknowledged_at == 1000.0
    assert second.acknowledged_at == 1000.0


def test_a_retried_put_never_erases_an_acknowledgement(store):
    # The generation may re-publish a terminal record after the owner already acked it.
    # rev 1's unconditional write reset acknowledged_at to None.
    sid = "s-" + "1" * 32
    store.put_terminal(make_terminal(sid))
    store.ack_terminal(sid, owner_id="hermes:local")
    store.put_terminal(make_terminal(sid))
    assert store.get_terminal(sid, owner_id="hermes:local").acknowledged_at == 1000.0


def test_concurrent_acks_agree_on_one_timestamp(tmp_path):
    # rev 1's ack was a read-modify-write: two callers both saw None, both stamped, and the
    # later write won — so "the original timestamp never changes" was false under the only
    # conditions that matter. Sequential tests cannot see this.
    ticks = iter(range(1, 10_000))
    store = Store(tmp_path, clock=lambda: float(next(ticks)))
    sid = "s-" + "1" * 32
    store.put_terminal(make_terminal(sid))
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


def test_prune_removes_acknowledged_records(store):
    sid = "s-" + "1" * 32
    store.put_terminal(make_terminal(sid))
    store.ack_terminal(sid, owner_id="hermes:local")
    assert store.prune_terminal(max_age_seconds=86400, max_count=100) == 1
    with pytest.raises(NelixError):
        store.get_terminal(sid, owner_id="hermes:local")


def test_prune_reaps_an_abandoned_record_past_max_age(store):
    store.put_terminal(make_terminal("s-" + "1" * 32, ended_at=0.0))
    assert store.prune_terminal(max_age_seconds=500, max_count=100) == 1


def test_prune_bounds_by_count_dropping_oldest_first(store):
    for i in range(5):
        store.put_terminal(make_terminal(f"s-{i}" + "0" * 31, ended_at=float(i)))
    assert store.prune_terminal(max_age_seconds=86400, max_count=2) == 3
    assert sorted(r.ended_at for r in store.list_terminal("hermes:local")) == [3.0, 4.0]


def test_a_noisy_owner_cannot_evict_a_quiet_owners_unacked_result(store):
    # THE rev 1 Critical, probe-proven by review: the count bound was applied across ALL
    # owners, so one owner's churn deleted another's unacknowledged result — violating both
    # "unacked results survive" and "owner is a correctness namespace".
    store.put_terminal(make_terminal("s-" + "9" * 32, owner="quiet:1", ended_at=1.0))
    for i in range(5):
        store.put_terminal(make_terminal(f"s-{i}" + "7" * 31, owner="noisy:1",
                                         ended_at=float(100 + i)))
    store.prune_terminal(max_age_seconds=86400, max_count=3)
    assert store.get_terminal("s-" + "9" * 32, owner_id="quiet:1").ended_at == 1.0
    assert len(store.list_terminal("noisy:1")) == 3


def test_prune_ties_break_deterministically(store):
    for sid in ("s-" + "a" * 32, "s-" + "b" * 32, "s-" + "c" * 32):
        store.put_terminal(make_terminal(sid, ended_at=5.0))
    store.prune_terminal(max_age_seconds=86400, max_count=1)
    assert [r.session_id for r in store.list_terminal("hermes:local")] == ["s-" + "c" * 32]


@pytest.mark.parametrize("kwargs", [{"max_age_seconds": -1, "max_count": 1},
                                    {"max_age_seconds": 1, "max_count": -1}])
def test_prune_rejects_nonsense_bounds(store, kwargs):
    with pytest.raises(NelixError):
        store.prune_terminal(**kwargs)


def test_terminal_reads_are_owner_filtered(store):
    sid = "s-" + "1" * 32
    store.put_terminal(make_terminal(sid))
    with pytest.raises(NelixError) as ei:
        store.get_terminal(sid, owner_id="claude-code:1")
    assert ei.value.code == errors.OWNER_MISMATCH
    assert store.list_terminal("claude-code:1") == []


def test_one_future_schema_row_does_not_brick_an_owners_terminal_board(store):
    # The untested half of rev 2's Critical fix: list_sessions was covered, list_terminal
    # was not.
    sid = "s-" + "1" * 32
    store.put_terminal(make_terminal(sid))
    store._conn.execute(
        "INSERT INTO terminal (session_id, owner_id, orchestration_id, generation_id,"
        " terminal_kind, summary, ended_at, acknowledged_at, schema_version)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("s-" + "8" * 32, "hermes:local", OID, GID, "done", "s", 200.0, None, 99))
    assert [r.session_id for r in store.list_terminal("hermes:local")] == [sid]
    with pytest.raises(NelixError) as ei:
        store.get_terminal("s-" + "8" * 32, owner_id="hermes:local")
    assert ei.value.code == errors.SCHEMA_TOO_NEW


def test_a_corrupt_terminal_row_does_not_blind_an_owner(store):
    sid = "s-" + "1" * 32
    store.put_terminal(make_terminal(sid))
    store._conn.execute(
        "INSERT INTO terminal (session_id, owner_id, orchestration_id, generation_id,"
        " terminal_kind, summary, ended_at, acknowledged_at, schema_version)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("s-" + "7" * 32, "hermes:local", OID, GID, "done", "s", "soon", None, 1))
    assert [r.session_id for r in store.list_terminal("hermes:local")] == [sid]
