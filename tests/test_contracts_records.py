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
