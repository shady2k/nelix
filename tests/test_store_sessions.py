import json

import pytest

from nelix_contracts import errors
from nelix_contracts.errors import NelixError
from nelix_contracts.records import SessionRecord
from nelix_store.store import Store

OID = "o-" + "2" * 32
GID = "g-" + "3" * 32


def make_session(sid, owner="hermes:local", **over):
    fields = dict(session_id=sid, owner_id=owner, orchestration_id=OID, generation_id=GID,
                  state="starting", executor="coder", task="t", cwd="/repo",
                  model=None, created_at=100.0)
    fields.update(over)
    return SessionRecord(**fields)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path, clock=lambda: 1000.0)


def test_put_then_get_round_trips(store):
    sid = "s-" + "1" * 32
    rec = make_session(sid)
    store.put_session(rec)
    assert store.get_session(sid, owner_id="hermes:local") == rec


def test_get_rejects_another_owner(store):
    sid = "s-" + "1" * 32
    store.put_session(make_session(sid))
    with pytest.raises(NelixError) as ei:
        store.get_session(sid, owner_id="claude-code:1")
    assert ei.value.code == errors.OWNER_MISMATCH


def test_get_unknown_session_is_unknown_not_empty(store):
    with pytest.raises(NelixError) as ei:
        store.get_session("s-" + "9" * 32, owner_id="hermes:local")
    assert ei.value.code == errors.UNKNOWN_SESSION


def test_list_sessions_is_owner_filtered(store):
    store.put_session(make_session("s-" + "1" * 32, owner="hermes:local"))
    store.put_session(make_session("s-" + "2" * 32, owner="claude-code:1"))
    mine = store.list_sessions("hermes:local")
    assert [r.session_id for r in mine] == ["s-" + "1" * 32]


def test_put_is_atomic_leaving_no_partial_file(store, tmp_path):
    # An owner record is an access invariant: a half-written file must never be readable.
    store.put_session(make_session("s-" + "1" * 32))
    leftovers = [p.name for p in tmp_path.rglob("*") if p.suffix == ".tmp"]
    assert leftovers == []


def test_a_corrupt_record_fails_closed_rather_than_defaulting(store, tmp_path):
    sid = "s-" + "1" * 32
    store.put_session(make_session(sid))
    path = next(tmp_path.rglob(f"{sid}.json"))
    path.write_text("{not json")
    with pytest.raises(NelixError):
        store.get_session(sid, owner_id="hermes:local")


def test_put_overwrites_in_place(store):
    sid = "s-" + "1" * 32
    store.put_session(make_session(sid, state="starting"))
    store.put_session(make_session(sid, state="running"))
    assert store.get_session(sid, owner_id="hermes:local").state == "running"
