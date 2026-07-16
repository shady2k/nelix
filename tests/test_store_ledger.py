import threading

import pytest

from nelix_contracts import errors
from nelix_contracts.errors import NelixError
from nelix_store.ledger import StartLedger

OID = "o-" + "2" * 32
GID = "g-" + "3" * 32
GID2 = "g-" + "4" * 32
FP = "fingerprint-of-the-start-request"


@pytest.fixture
def ledger(tmp_path):
    lg = StartLedger(tmp_path, clock=lambda: 1000.0)
    yield lg
    lg.close()


def reserve(lg, key="k1", owner="hermes:local", fp=FP):
    return lg.reserve(idempotency_key=key, owner_id=owner, orchestration_id=OID,
                      request_fingerprint=fp)


def test_reserve_mints_a_session_id_before_any_generation_is_touched(ledger):
    r = reserve(ledger)
    assert r.session_id.startswith("s-")
    assert r.state == "starting"
    assert r.generation_id is None
    assert r.replay is False


def test_the_generation_is_persisted_before_forwarding(ledger):
    # THE rev 1 hole: without this, a lost start response left state=starting,
    # generation_id=None — so the retry could not recover against the ORIGINAL generation,
    # which is the entire ambiguity the ledger exists to close.
    r = reserve(ledger)
    ledger.assign_generation(r.session_id, GID)
    replay = reserve(ledger)
    assert replay.replay is True
    assert replay.generation_id == GID
    assert replay.state == "starting"


def test_replaying_a_key_returns_the_same_session_never_a_second_worker(ledger):
    first = reserve(ledger)
    ledger.assign_generation(first.session_id, GID)
    ledger.commit(first.session_id, GID)
    second = reserve(ledger)
    assert second.session_id == first.session_id
    assert second.replay is True
    assert second.state == "started"
    assert second.generation_id == GID


def test_the_same_key_with_a_different_request_is_a_conflict(ledger):
    # rev 1 compared only the key, so this silently returned the OLD operation — the caller
    # believed its new task had started.
    reserve(ledger, fp="fingerprint-A")
    with pytest.raises(NelixError) as ei:
        reserve(ledger, fp="fingerprint-B")
    assert ei.value.code == errors.IDEMPOTENCY_CONFLICT


def test_distinct_keys_mint_distinct_sessions(ledger):
    assert reserve(ledger, key="k1").session_id != reserve(ledger, key="k2").session_id


def test_two_owners_may_use_the_same_key_string_independently(ledger):
    # Keys are namespaced per owner. rev 1 reserved key STRINGS globally, so owner B using
    # "deploy" locked owner A out of its own key.
    a = reserve(ledger, owner="hermes:local")
    b = reserve(ledger, owner="claude-code:1")
    assert a.session_id != b.session_id
    assert b.replay is False


def test_exactly_one_reservation_survives_a_race(tmp_path):
    # rev 1's check-then-write could interleave and mint TWO session ids for one key — two
    # workers for one task. No sequential test can see this.
    results, errs, barrier = [], [], threading.Barrier(8)

    def go():
        lg = StartLedger(tmp_path, clock=lambda: 1000.0)
        barrier.wait()
        try:
            results.append(reserve(lg).session_id)
        except NelixError as e:      # a loser may legitimately see a conflict
            errs.append(e.code)
        finally:
            lg.close()

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs, f"unexpected errors: {errs}"
    assert len(set(results)) == 1, f"the race minted {len(set(results))} sessions: {set(results)}"


def test_lookup_is_owner_guarded(ledger):
    # rev 1's lookup took no owner_id at all: reserve() correctly refused a cross-owner key
    # and then lookup handed any caller the session_id, state and generation 40 lines later.
    reserve(ledger, owner="hermes:local")
    assert ledger.lookup("k1", owner_id="claude-code:1") is None
    assert ledger.lookup("k1", owner_id="hermes:local").session_id.startswith("s-")


def test_lookup_of_an_unknown_key_is_none(ledger):
    assert ledger.lookup("nope", owner_id="hermes:local") is None


def test_a_failed_start_carries_its_reason_on_replay(ledger):
    r = reserve(ledger)
    ledger.fail(r.session_id, "bad cwd")
    again = reserve(ledger)
    assert again.state == "failed"
    assert again.reason == "bad cwd"      # rev 1 discarded the stored reason


def test_committing_twice_to_the_same_generation_is_idempotent(ledger):
    r = reserve(ledger)
    ledger.assign_generation(r.session_id, GID)
    ledger.commit(r.session_id, GID)
    ledger.commit(r.session_id, GID)
    assert reserve(ledger).state == "started"


def test_committing_to_a_different_generation_is_a_conflict(ledger):
    # rev 1 had no state machine: a double commit silently REBOUND the session to another
    # generation.
    r = reserve(ledger)
    ledger.assign_generation(r.session_id, GID)
    ledger.commit(r.session_id, GID)
    with pytest.raises(NelixError) as ei:
        ledger.commit(r.session_id, GID2)
    assert ei.value.code == errors.IDEMPOTENCY_CONFLICT


def test_failing_an_already_started_session_is_refused(ledger):
    r = reserve(ledger)
    ledger.assign_generation(r.session_id, GID)
    ledger.commit(r.session_id, GID)
    with pytest.raises(NelixError):
        ledger.fail(r.session_id, "too late")


def test_committing_an_already_failed_session_is_refused(ledger):
    r = reserve(ledger)
    ledger.fail(r.session_id, "bad cwd")
    with pytest.raises(NelixError):
        ledger.commit(r.session_id, GID)


def test_commit_of_an_unknown_session_is_rejected(ledger):
    with pytest.raises(NelixError) as ei:
        ledger.commit("s-" + "9" * 32, GID)
    assert ei.value.code == errors.UNKNOWN_SESSION


@pytest.mark.parametrize("key", [None, 123, "", {}])
def test_a_malformed_idempotency_key_is_a_contract_error_not_a_crash(ledger, key):
    with pytest.raises(NelixError):
        ledger.reserve(idempotency_key=key, owner_id="hermes:local",
                       orchestration_id=OID, request_fingerprint=FP)


def test_the_ledger_survives_a_restart(tmp_path):
    lg = StartLedger(tmp_path, clock=lambda: 1000.0)
    first = reserve(lg)
    lg.close()
    lg2 = StartLedger(tmp_path, clock=lambda: 2000.0)
    assert lg2.lookup("k1", owner_id="hermes:local").session_id == first.session_id
    lg2.close()
