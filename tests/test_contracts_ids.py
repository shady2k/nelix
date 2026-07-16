import pytest

from nelix_contracts.ids import (
    InvalidId, new_generation_id, new_orchestration_id, new_session_id,
    validate_generation_id, validate_orchestration_id, validate_owner_id, validate_session_id,
)


def test_session_id_carries_full_uuid_entropy():
    # 32 hex chars = 128 bits. The old daemon minted uuid4().hex[:8] (32 BITS), which is
    # collision-prone for a long-lived, multi-generation namespace that also holds archived
    # sessions. This test is the guard against re-narrowing it.
    sid = new_session_id()
    assert sid.startswith("s-")
    assert len(sid) == 2 + 32
    validate_session_id(sid)


def test_ids_are_unique_across_mints():
    assert len({new_session_id() for _ in range(1000)}) == 1000


def test_each_kind_has_its_own_prefix():
    assert new_orchestration_id().startswith("o-")
    assert new_generation_id().startswith("g-")
    validate_orchestration_id(new_orchestration_id())
    validate_generation_id(new_generation_id())


def test_legacy_eight_hex_session_id_is_rejected():
    # Explicitly refuse the old short form so a stale caller cannot smuggle it in.
    with pytest.raises(InvalidId):
        validate_session_id("s-93008e08")


@pytest.mark.parametrize("bad", ["", "s-", "93008e08", "o-" + "a" * 32, "s-" + "A" * 32,
                                 "s-" + "g" * 32, None, 42])
def test_validate_session_id_rejects_junk(bad):
    with pytest.raises(InvalidId):
        validate_session_id(bad)


def test_owner_id_accepts_a_durable_installation_identity():
    # An owner is a durable adapter/profile installation — not a pid, not a conversation.
    assert validate_owner_id("hermes:local") == "hermes:local"
    assert validate_owner_id("claude-code.7f3a9b") == "claude-code.7f3a9b"


@pytest.mark.parametrize("bad", ["", " ", "-leading", "has space", "x" * 129, None, 7])
def test_validate_owner_id_rejects_junk(bad):
    with pytest.raises(InvalidId):
        validate_owner_id(bad)
