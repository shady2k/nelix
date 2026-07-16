import pytest

from nelix_contracts import errors
from nelix_contracts.errors import NelixError
from nelix_store.ledger import StartLedger

OID = "o-" + "2" * 32
GID = "g-" + "3" * 32


@pytest.fixture
def ledger(tmp_path):
    return StartLedger(tmp_path, clock=lambda: 1000.0)


def test_reserve_mints_a_session_id_before_any_generation_is_touched(ledger):
    # The router assigns the id BEFORE forwarding /start. Two reasons (design §3): a worker
    # hook can fire before /start's response comes back, and the router must already know
    # where to route it; and a lost response must not spawn a second worker.
    r = ledger.reserve(idempotency_key="k1", owner_id="hermes:local", orchestration_id=OID)
    assert r.session_id.startswith("s-")
    assert r.state == "starting"
    assert r.replay is False


def test_replaying_a_key_returns_the_same_session_never_a_second_worker(ledger):
    # THE guard: start succeeded but the reply was lost, so the caller retries.
    first = ledger.reserve(idempotency_key="k1", owner_id="hermes:local",
                           orchestration_id=OID)
    ledger.commit(first.session_id, GID)
    second = ledger.reserve(idempotency_key="k1", owner_id="hermes:local",
                            orchestration_id=OID)
    assert second.session_id == first.session_id
    assert second.replay is True
    assert second.state == "started"
    assert second.generation_id == GID


def test_replay_of_an_in_flight_reservation_does_not_mint_a_new_id(ledger):
    first = ledger.reserve(idempotency_key="k1", owner_id="hermes:local",
                           orchestration_id=OID)
    second = ledger.reserve(idempotency_key="k1", owner_id="hermes:local",
                            orchestration_id=OID)
    assert second.session_id == first.session_id
    assert second.replay is True
    assert second.state == "starting"


def test_distinct_keys_mint_distinct_sessions(ledger):
    a = ledger.reserve(idempotency_key="k1", owner_id="hermes:local", orchestration_id=OID)
    b = ledger.reserve(idempotency_key="k2", owner_id="hermes:local", orchestration_id=OID)
    assert a.session_id != b.session_id


def test_another_owner_cannot_hijack_a_key(ledger):
    # Keys are namespaced per owner: owner B replaying owner A's key must not be handed A's
    # session.
    ledger.reserve(idempotency_key="k1", owner_id="hermes:local", orchestration_id=OID)
    with pytest.raises(NelixError) as ei:
        ledger.reserve(idempotency_key="k1", owner_id="claude-code:1", orchestration_id=OID)
    assert ei.value.code == errors.OWNER_MISMATCH


def test_a_failed_start_is_recorded_and_replayable(ledger):
    r = ledger.reserve(idempotency_key="k1", owner_id="hermes:local", orchestration_id=OID)
    ledger.fail(r.session_id, "bad cwd")
    again = ledger.reserve(idempotency_key="k1", owner_id="hermes:local",
                           orchestration_id=OID)
    assert again.state == "failed"
    assert again.replay is True


def test_lookup_of_an_unknown_key_is_none(ledger):
    assert ledger.lookup("nope") is None


def test_commit_of_an_unknown_session_is_rejected(ledger):
    with pytest.raises(NelixError) as ei:
        ledger.commit("s-" + "9" * 32, GID)
    assert ei.value.code == errors.UNKNOWN_SESSION


def test_the_ledger_survives_a_restart(tmp_path):
    # The router may be replaced mid-start; the ledger is the durable record of what was
    # already promised.
    first = StartLedger(tmp_path, clock=lambda: 1000.0).reserve(
        idempotency_key="k1", owner_id="hermes:local", orchestration_id=OID)
    reopened = StartLedger(tmp_path, clock=lambda: 2000.0).lookup("k1")
    assert reopened.session_id == first.session_id
