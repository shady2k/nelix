import pytest

from nelix_contracts import errors
from nelix_contracts.errors import NelixError
from nelix_contracts.records import (
    SCHEMA_VERSION, SessionRecord, TerminalRecord, assert_owner,
)

SID = "s-" + "1" * 32
OID = "o-" + "2" * 32
GID = "g-" + "3" * 32


def make_session(**over):
    fields = dict(session_id=SID, owner_id="hermes:local", orchestration_id=OID,
                  generation_id=GID, state="starting", executor="coder",
                  task="fix login", cwd="/repo", model=None, created_at=100.0)
    fields.update(over)
    return SessionRecord(**fields)


def test_session_record_round_trips():
    rec = make_session()
    assert SessionRecord.from_dict(rec.to_dict()) == rec


def test_record_carries_its_schema_version():
    assert make_session().to_dict()["schema_version"] == SCHEMA_VERSION


def test_reading_a_future_schema_fails_closed():
    # An OLDER generation must never silently misread a NEWER generation's record — both
    # read the same store. Generations remove live-state compatibility, not durable-data
    # schema compatibility (design §5).
    raw = make_session().to_dict()
    raw["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(NelixError) as ei:
        SessionRecord.from_dict(raw)
    assert ei.value.code == errors.SCHEMA_TOO_NEW


def test_record_with_a_malformed_id_is_rejected():
    with pytest.raises(NelixError) as ei:
        SessionRecord.from_dict({**make_session().to_dict(), "session_id": "s-93008e08"})
    assert ei.value.code == errors.INVALID_REQUEST


def test_terminal_record_round_trips_unacknowledged():
    rec = TerminalRecord(session_id=SID, owner_id="hermes:local", orchestration_id=OID,
                         generation_id=GID, terminal_kind="done", summary="all green",
                         ended_at=500.0)
    assert rec.acknowledged_at is None
    assert TerminalRecord.from_dict(rec.to_dict()) == rec


def test_assert_owner_passes_for_the_owner():
    assert_owner(make_session(), "hermes:local") is None


def test_assert_owner_rejects_another_harness():
    # The guard behind EVERY caller-facing route: a session id alone must never be enough.
    with pytest.raises(NelixError) as ei:
        assert_owner(make_session(), "claude-code:1")
    assert ei.value.code == errors.OWNER_MISMATCH


@pytest.mark.parametrize("field,bad", [
    ("state", []), ("state", None), ("executor", None), ("task", 42),
    ("cwd", 7), ("created_at", "yesterday"), ("created_at", float("nan")),
    ("created_at", float("inf")), ("created_at", True), ("model", 3),
    ("schema_version", True), ("schema_version", 0), ("schema_version", "1"),
])
def test_session_record_rejects_a_malformed_field_at_construction(field, bad):
    # Not just via from_dict: a record object must be valid by construction, or a corrupt
    # write surfaces far from its cause (in prune, in sorting, in the board).
    with pytest.raises(NelixError) as ei:
        make_session(**{field: bad})
    assert ei.value.code == errors.INVALID_REQUEST


def test_session_record_accepts_a_null_model():
    assert make_session(model=None).model is None
    assert make_session(model="opus").model == "opus"


@pytest.mark.parametrize("field,bad", [
    ("terminal_kind", None), ("summary", 5), ("ended_at", "soon"),
    ("ended_at", float("nan")), ("ended_at", True), ("acknowledged_at", "yes"),
    ("acknowledged_at", float("inf")),
])
def test_terminal_record_rejects_a_malformed_field_at_construction(field, bad):
    fields = dict(session_id=SID, owner_id="hermes:local", orchestration_id=OID,
                  generation_id=GID, terminal_kind="done", summary="ok", ended_at=1.0)
    fields[field] = bad
    with pytest.raises(NelixError) as ei:
        TerminalRecord(**fields)
    assert ei.value.code == errors.INVALID_REQUEST


def test_a_malformed_id_is_rejected_at_construction_too():
    with pytest.raises(NelixError) as ei:
        make_session(session_id="s-93008e08")
    assert ei.value.code == errors.INVALID_REQUEST
