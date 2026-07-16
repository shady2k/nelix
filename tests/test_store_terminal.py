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
    return Store(tmp_path, clock=clock)


def test_an_unacknowledged_result_survives_far_past_the_old_300s_ttl(store, clock):
    # THE defect this fixes: the live daemon expires terminal snapshots after
    # terminal_snapshot_ttl=300.0 and sweeps them on every global status, so a harness away
    # for six minutes came back to a vanished result. "Come back later" is the product's
    # whole promise.
    sid = "s-" + "1" * 32
    store.put_terminal(make_terminal(sid, ended_at=1000.0))
    clock.t = 1000.0 + 3600
    assert store.prune_terminal(max_age_seconds=86400, max_count=100) == 0
    assert store.get_terminal(sid, owner_id="hermes:local").terminal_kind == "done"


def test_ack_is_idempotent(store, clock):
    # The owner may retry an ack after a lost reply; the second must be a success, not an error.
    sid = "s-" + "1" * 32
    store.put_terminal(make_terminal(sid))
    first = store.ack_terminal(sid, owner_id="hermes:local")
    clock.t = 2000.0
    second = store.ack_terminal(sid, owner_id="hermes:local")
    assert first.acknowledged_at == 1000.0
    assert second.acknowledged_at == 1000.0     # not re-stamped


def test_prune_removes_acknowledged_records(store):
    sid = "s-" + "1" * 32
    store.put_terminal(make_terminal(sid))
    store.ack_terminal(sid, owner_id="hermes:local")
    assert store.prune_terminal(max_age_seconds=86400, max_count=100) == 1
    with pytest.raises(NelixError):
        store.get_terminal(sid, owner_id="hermes:local")


def test_prune_reaps_an_abandoned_record_past_max_age(store):
    # An owner that never returns must not grow storage forever.
    store.put_terminal(make_terminal("s-" + "1" * 32, ended_at=0.0))
    assert store.prune_terminal(max_age_seconds=500, max_count=100) == 1


def test_prune_bounds_by_count_dropping_oldest_first(store):
    for i in range(5):
        store.put_terminal(make_terminal(f"s-{i}" + "0" * 31, ended_at=float(i)))
    assert store.prune_terminal(max_age_seconds=86400, max_count=2) == 3
    kept = sorted(r.ended_at for r in store.list_terminal("hermes:local"))
    assert kept == [3.0, 4.0]


def test_terminal_reads_are_owner_filtered(store):
    sid = "s-" + "1" * 32
    store.put_terminal(make_terminal(sid))
    with pytest.raises(NelixError) as ei:
        store.get_terminal(sid, owner_id="claude-code:1")
    assert ei.value.code == errors.OWNER_MISMATCH
    assert store.list_terminal("claude-code:1") == []
