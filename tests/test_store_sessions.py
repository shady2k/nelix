import pytest

from nelix_contracts import errors
from nelix_contracts.errors import NelixError
from nelix_contracts.records import SessionRecord
from nelix_store.store import Store

OID = "o-" + "2" * 32
GID = "g-" + "3" * 32
SID = "s-" + "1" * 32


def make_session(sid=SID, owner="hermes:local", **over):
    fields = dict(session_id=sid, owner_id=owner, orchestration_id=OID, generation_id=GID,
                  state="starting", executor="coder", task="t", cwd="/repo",
                  model=None, created_at=100.0)
    fields.update(over)
    return SessionRecord(**fields)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path, clock=lambda: 1000.0)
    yield s
    s.close()


def test_create_then_get_round_trips(store):
    rec = make_session()
    store.create_session(rec)
    assert store.get_session(SID, owner_id="hermes:local") == rec


def test_create_never_overwrites_an_existing_session(store):
    # rev 1's put_session replaced the whole record, so a second write could hand the
    # session to ANOTHER OWNER. Creation is exclusive; identity is immutable.
    store.create_session(make_session())
    with pytest.raises(NelixError) as ei:
        store.create_session(make_session(owner="claude-code:1"))
    assert ei.value.code == errors.DUPLICATE_START
    assert store.get_session(SID, owner_id="hermes:local").owner_id == "hermes:local"


def test_transition_changes_state_and_nothing_else(store):
    store.create_session(make_session(state="starting"))
    store.transition_session(SID, owner_id="hermes:local", state="running")
    rec = store.get_session(SID, owner_id="hermes:local")
    assert rec.state == "running"
    assert (rec.owner_id, rec.orchestration_id, rec.generation_id, rec.task,
            rec.cwd, rec.created_at) == ("hermes:local", OID, GID, "t", "/repo", 100.0)


def test_transition_is_owner_guarded(store):
    store.create_session(make_session())
    with pytest.raises(NelixError) as ei:
        store.transition_session(SID, owner_id="claude-code:1", state="running")
    assert ei.value.code == errors.OWNER_MISMATCH
    assert store.get_session(SID, owner_id="hermes:local").state == "starting"


def test_get_rejects_another_owner(store):
    store.create_session(make_session())
    with pytest.raises(NelixError) as ei:
        store.get_session(SID, owner_id="claude-code:1")
    assert ei.value.code == errors.OWNER_MISMATCH


def test_get_unknown_session_is_unknown_not_empty(store):
    with pytest.raises(NelixError) as ei:
        store.get_session("s-" + "9" * 32, owner_id="hermes:local")
    assert ei.value.code == errors.UNKNOWN_SESSION


def test_list_sessions_is_owner_filtered(store):
    store.create_session(make_session(SID, owner="hermes:local"))
    store.create_session(make_session("s-" + "2" * 32, owner="claude-code:1"))
    assert [r.session_id for r in store.list_sessions("hermes:local")] == [SID]


def test_one_future_schema_row_does_not_brick_an_owners_board(store):
    # THE rev 1 Critical: a newer generation writes a v2 record — the DESIGNED steady state
    # during an upgrade — and rev 1's list_sessions raised schema_too_new for the whole call,
    # so the owner could not read even their OWN v1 rows. get() must still fail closed on
    # that specific row; list() must skip it and return the rest.
    store.create_session(make_session(SID))
    store._conn.execute(
        "INSERT INTO sessions (session_id, owner_id, orchestration_id, generation_id, state,"
        " executor, task, cwd, model, created_at, schema_version)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("s-" + "8" * 32, "hermes:local", OID, GID, "running", "coder", "t", "/repo",
         None, 200.0, 99))
    assert [r.session_id for r in store.list_sessions("hermes:local")] == [SID]
    with pytest.raises(NelixError) as ei:
        store.get_session("s-" + "8" * 32, owner_id="hermes:local")
    assert ei.value.code == errors.SCHEMA_TOO_NEW


def test_a_non_duplicate_integrity_failure_is_not_reported_as_a_duplicate_start(store):
    # NOT NULL is not a duplicate. Mapping every IntegrityError to DUPLICATE_START tells the
    # caller to stop retrying a start that never conflicted.
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO sessions (session_id, owner_id, orchestration_id, generation_id,"
            " state, executor, task, cwd, model, created_at, schema_version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (SID, None, OID, GID, "starting", "coder", "t", "/repo", None, 1.0, 1))
