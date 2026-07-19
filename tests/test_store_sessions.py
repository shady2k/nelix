import pytest

from nelix_contracts import errors
from nelix_contracts.errors import NelixError
from nelix_store.ledger import StartLedger
from nelix_store.store import Store

OID = "o-" + "2" * 32
GID = "g-" + "3" * 32
GEPOCH = "g-" + "6" * 32
SID = "s-" + "1" * 32


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path, clock=lambda: 1000.0)
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


def test_a_session_cannot_be_created_for_a_start_that_does_not_exist(store):
    # Identity is derived from the reservation. A session with no start is an orphan whose
    # owner nobody can establish.
    with pytest.raises(NelixError) as ei:
        store.create_session(SID, state="starting", executor="coder", task="t",
                             cwd="/repo", model=None, created_at=100.0)
    assert ei.value.code == errors.UNKNOWN_SESSION


def test_a_session_inherits_its_identity_from_its_start(store, ledger):
    # The caller supplies NO owner/orchestration/generation — there is no way for them to
    # disagree with the start, because they are never given a chance to.
    #
    # TWO owners, each with their own start + session, and DISTINCT orchestration/generation
    # ids: a single owner cannot catch a join hardcoded to a constant owner (e.g.
    # `_SESSION_SELECT` joining `starts` on `st.owner_id = 'hermes:local'` instead of
    # `st.session_id = s.session_id`) — with only one owner in the store, that literal is
    # indistinguishable from a correct join. This mirrors the same gap documented for
    # `_TERMINAL_SELECT` in f1k-rev4-report.md (mutation 4); `_SESSION_SELECT` has the
    # identical join shape, so it carries the identical risk.
    oid_b = "o-" + "4" * 32
    gid_b = "g-" + "5" * 32

    r_a = ledger.reserve(idempotency_key="k-a", owner_id="hermes:local",
                         orchestration_id=OID, request_fingerprint="fp")
    ledger.assign_generation(r_a.session_id, GID, GEPOCH)
    store.create_session(r_a.session_id, state="starting", executor="coder", task="t",
                         cwd="/repo", model=None, created_at=100.0)

    r_b = ledger.reserve(idempotency_key="k-b", owner_id="claude-code:1",
                         orchestration_id=oid_b, request_fingerprint="fp")
    ledger.assign_generation(r_b.session_id, gid_b, GEPOCH)
    store.create_session(r_b.session_id, state="starting", executor="coder", task="t",
                         cwd="/repo", model=None, created_at=100.0)

    rec_a = store.get_session(r_a.session_id, owner_id="hermes:local")
    assert (rec_a.owner_id, rec_a.orchestration_id, rec_a.generation_id) == (
        "hermes:local", OID, GID)

    rec_b = store.get_session(r_b.session_id, owner_id="claude-code:1")
    assert (rec_b.owner_id, rec_b.orchestration_id, rec_b.generation_id) == (
        "claude-code:1", oid_b, gid_b)

    assert [s.session_id for s in store.list_sessions("hermes:local")] == [r_a.session_id]
    assert [s.session_id for s in store.list_sessions("claude-code:1")] == [r_b.session_id]


def test_a_session_cannot_be_created_before_its_generation_is_assigned(store, ledger):
    # create_session derives generation_id from the start; an unassigned start has none, and
    # a session must never exist without the generation that runs it.
    r = ledger.reserve(idempotency_key="k1", owner_id="hermes:local",
                       orchestration_id=OID, request_fingerprint="fp")
    with pytest.raises(NelixError) as ei:
        store.create_session(r.session_id, state="starting", executor="coder", task="t",
                             cwd="/repo", model=None, created_at=100.0)
    assert ei.value.code == errors.IDEMPOTENCY_CONFLICT


def test_create_then_get_round_trips(store, ledger):
    sid = started_session(store, ledger)
    rec = store.get_session(sid, owner_id="hermes:local")
    assert (rec.session_id, rec.owner_id, rec.orchestration_id, rec.generation_id, rec.state,
            rec.executor, rec.task, rec.cwd, rec.model, rec.created_at) == (
        sid, "hermes:local", OID, GID, "starting", "coder", "t", "/repo", None, 100.0)


def test_create_never_overwrites_an_existing_session(store, ledger):
    # rev 1's put_session replaced the whole record, so a second write could hand the
    # session to ANOTHER OWNER. Creation is exclusive; identity is immutable.
    sid = started_session(store, ledger)
    with pytest.raises(NelixError) as ei:
        store.create_session(sid, state="starting", executor="coder", task="t",
                             cwd="/repo", model=None, created_at=100.0)
    assert ei.value.code == errors.DUPLICATE_START
    assert store.get_session(sid, owner_id="hermes:local").owner_id == "hermes:local"


def test_transition_changes_state_and_nothing_else(store, ledger):
    sid = started_session(store, ledger, state="starting")
    store.transition_session(sid, owner_id="hermes:local", state="running")
    rec = store.get_session(sid, owner_id="hermes:local")
    assert rec.state == "running"
    assert (rec.owner_id, rec.orchestration_id, rec.generation_id, rec.task,
            rec.cwd, rec.created_at) == ("hermes:local", OID, GID, "t", "/repo", 100.0)


def test_transition_is_owner_guarded(store, ledger):
    sid = started_session(store, ledger)
    with pytest.raises(NelixError) as ei:
        store.transition_session(sid, owner_id="claude-code:1", state="running")
    assert ei.value.code == errors.OWNER_MISMATCH
    assert store.get_session(sid, owner_id="hermes:local").state == "starting"


def test_get_rejects_another_owner(store, ledger):
    sid = started_session(store, ledger)
    with pytest.raises(NelixError) as ei:
        store.get_session(sid, owner_id="claude-code:1")
    assert ei.value.code == errors.OWNER_MISMATCH


def test_get_unknown_session_is_unknown_not_empty(store):
    with pytest.raises(NelixError) as ei:
        store.get_session("s-" + "9" * 32, owner_id="hermes:local")
    assert ei.value.code == errors.UNKNOWN_SESSION


def test_list_sessions_is_owner_filtered(store, ledger):
    sid = started_session(store, ledger, owner="hermes:local", key="k1")
    started_session(store, ledger, owner="claude-code:1", key="k2")
    assert [r.session_id for r in store.list_sessions("hermes:local")] == [sid]


def test_one_future_schema_row_does_not_brick_an_owners_board(store, ledger):
    # THE rev 1 Critical: a newer generation writes a v2 record — the DESIGNED steady state
    # during an upgrade — and rev 1's list_sessions raised schema_too_new for the whole call,
    # so the owner could not read even their OWN v1 rows. get() must still fail closed on
    # that specific row; list() must skip it and return the rest.
    sid = started_session(store, ledger, key="k1")
    other = ledger.reserve(idempotency_key="k2", owner_id="hermes:local",
                           orchestration_id=OID, request_fingerprint="fp")
    ledger.assign_generation(other.session_id, GID, GEPOCH)
    store._conn.execute(
        "INSERT INTO sessions (session_id, state, executor, task, cwd, model, created_at,"
        " schema_version) VALUES (?,?,?,?,?,?,?,?)",
        (other.session_id, "running", "coder", "t", "/repo", None, 200.0, 99))
    assert [r.session_id for r in store.list_sessions("hermes:local")] == [sid]
    with pytest.raises(NelixError) as ei:
        store.get_session(other.session_id, owner_id="hermes:local")
    assert ei.value.code == errors.SCHEMA_TOO_NEW


def test_a_non_duplicate_integrity_failure_is_not_reported_as_a_duplicate_start(store, ledger):
    # f1k-rev5: this used to assert STORE_CORRUPT — a reviewer found that classification
    # wrong. state=None is MALFORMED CALLER INPUT, not durable-state damage; the party at
    # fault is the caller, so the code must be INVALID_REQUEST (Global Constraints: "caller's
    # malformed input -> INVALID_REQUEST"). Before this task, state=None reached SQLite's NOT
    # NULL constraint and came back as a raw sqlite3.IntegrityError, mapped here to
    # STORE_CORRUPT because it wasn't a UNIQUE/PRIMARY KEY violation. Now create_session
    # constructs a SessionRecord BEFORE writing, so state=None is caught by validation and
    # never reaches SQLite at all — the non-duplicate-IntegrityError branch this test
    # exercised is no longer reachable through the public API with any NOT-NULL column, since
    # every one of them (state, executor, task, cwd, created_at) is now record-validated
    # first. That branch is retained in store.py purely as defence for a corruption mode this
    # test can no longer trigger; nothing here still exercises it.
    r = ledger.reserve(idempotency_key="k1", owner_id="hermes:local",
                       orchestration_id=OID, request_fingerprint="fp")
    ledger.assign_generation(r.session_id, GID, GEPOCH)
    with pytest.raises(NelixError) as ei:
        store.create_session(r.session_id, state=None, executor="coder", task="t",
                             cwd="/repo", model=None, created_at=1.0)
    assert ei.value.code == errors.INVALID_REQUEST


def test_a_corrupt_row_does_not_blind_an_owner_to_their_healthy_rows(store, ledger):
    # SQLite has AFFINITY, not types: a REAL column stores 'yesterday' verbatim. The row
    # passes the schema filter and then explodes in from_dict — so rev 2's list_sessions
    # raised and the owner lost their whole board to one bad row.
    sid = started_session(store, ledger, key="k1")
    other = ledger.reserve(idempotency_key="k2", owner_id="hermes:local",
                           orchestration_id=OID, request_fingerprint="fp")
    ledger.assign_generation(other.session_id, GID, GEPOCH)
    store._conn.execute(
        "INSERT INTO sessions (session_id, state, executor, task, cwd, model, created_at,"
        " schema_version) VALUES (?,?,?,?,?,?,?,?)",
        (other.session_id, "running", "coder", "t", "/repo", None, "yesterday", 1))
    assert [r.session_id for r in store.list_sessions("hermes:local")] == [sid]


def test_transition_rejects_a_state_the_records_layer_would_reject(store, ledger):
    sid = started_session(store, ledger)
    with pytest.raises(NelixError) as ei:
        store.transition_session(sid, owner_id="hermes:local", state=42)
    assert ei.value.code == errors.INVALID_REQUEST
    assert store.get_session(sid, owner_id="hermes:local").state == "starting"


def test_a_failed_start_cannot_acquire_a_session(store, ledger):
    # THE Critical. The router calls fail() when a forward times out — exactly when the
    # generation may have created the session anyway. If the store accepts the session, the
    # caller retries the key, the ledger says "failed", the caller believes nothing started,
    # and dispatches a SECOND WORKER. That is the fatal case ledger.py's docstring names.
    r = ledger.reserve(idempotency_key="k1", owner_id="hermes:local",
                       orchestration_id=OID, request_fingerprint="fp")
    ledger.assign_generation(r.session_id, GID, GEPOCH)
    ledger.fail(r.session_id, "forward timed out")
    with pytest.raises(NelixError) as ei:
        store.create_session(r.session_id, state="running", executor="coder", task="t",
                             cwd="/repo", model=None, created_at=100.0)
    assert ei.value.code == errors.IDEMPOTENCY_CONFLICT
    with pytest.raises(NelixError) as ei2:
        store.get_session(r.session_id, owner_id="hermes:local")
    assert ei2.value.code == errors.UNKNOWN_SESSION


@pytest.mark.parametrize("field,bad", [
    ("executor", 42), ("task", 42), ("cwd", None), ("model", 42),
    ("created_at", "yesterday"), ("created_at", float("nan")),
    ("created_at", float("inf")), ("state", 42),
])
def test_create_session_rejects_a_malformed_field(store, ledger, field, bad):
    # rev 3 made records validate every field so corruption surfaces at its CAUSE. rev 4's
    # scalar API wrote caller values straight to SQLite, where TEXT affinity coerces 42 to
    # '42' and 'yesterday' persists in a REAL column — then explodes far away in list_*.
    sid = _assigned_start(ledger)
    fields = dict(state="starting", executor="coder", task="t", cwd="/repo",
                  model=None, created_at=100.0)
    fields[field] = bad
    with pytest.raises(NelixError) as ei:
        store.create_session(sid, **fields)
    assert ei.value.code == errors.INVALID_REQUEST


def _assigned_start(ledger, owner="hermes:local", key="k9"):
    r = ledger.reserve(idempotency_key=key, owner_id=owner, orchestration_id=OID,
                       request_fingerprint="fp")
    ledger.assign_generation(r.session_id, GID, GEPOCH)
    return r.session_id


def test_transition_can_be_conditional_on_the_expected_state(store, ledger):
    sid = started_session(store, ledger, state="starting")
    store.transition_session(sid, owner_id="hermes:local", state="running",
                             expected_state="starting")
    with pytest.raises(NelixError) as ei:
        store.transition_session(sid, owner_id="hermes:local", state="done",
                                 expected_state="starting")   # stale
    assert ei.value.code == errors.IDEMPOTENCY_CONFLICT
    assert store.get_session(sid, owner_id="hermes:local").state == "running"


def test_a_corrupt_stored_row_is_store_corrupt_not_the_callers_fault(store, ledger):
    # from_dict is a contract decoder: INVALID_REQUEST is right for a caller's bad input. But
    # a row read from DURABLE STORAGE that will not decode means the STORE is damaged. Telling
    # the caller "your request is invalid" sends them to fix the wrong thing.
    sid = started_session(store, ledger)
    store._conn.execute("UPDATE sessions SET created_at='yesterday' WHERE session_id=?", (sid,))
    with pytest.raises(NelixError) as ei:
        store.get_session(sid, owner_id="hermes:local")
    assert ei.value.code == errors.STORE_CORRUPT


def test_a_future_schema_row_still_reports_schema_too_new_not_corrupt(store, ledger):
    # SCHEMA_TOO_NEW must survive the reclassification: "written by a newer build" is a
    # different, actionable condition from "damaged".
    sid = started_session(store, ledger)
    store._conn.execute("UPDATE sessions SET schema_version=99 WHERE session_id=?", (sid,))
    with pytest.raises(NelixError) as ei:
        store.get_session(sid, owner_id="hermes:local")
    assert ei.value.code == errors.SCHEMA_TOO_NEW
