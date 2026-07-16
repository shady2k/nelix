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
    # rev 2's check-then-write could interleave and mint TWO session ids for one key — two
    # workers for one task. No sequential test can see this.
    #
    # NOTE the assertions: rev 2 asserted only `not errs` and `len(set(results)) == 1`, which
    # a single surviving thread satisfies. Seven threads could crash and the test still
    # passed, its only signal a thread-exception warning this project dismisses. Every thread
    # must be ACCOUNTED FOR.
    #
    # Bootstrap ONCE, outside the race. This test's invariant is "one reservation per
    # (owner, key)", not "concurrent first-open works" — that is test_store_db.py's job.
    # Racing both at once is what made rev 3's mutation signal unreadable.
    StartLedger(tmp_path, clock=lambda: 1000.0).close()

    results, errs, barrier = [], [], threading.Barrier(8)

    def go():
        try:
            lg = StartLedger(tmp_path, clock=lambda: 1000.0)
        except BaseException as e:          # noqa: BLE001 - account for EVERYTHING
            errs.append(f"open: {type(e).__name__}: {e}")
            barrier.wait(timeout=30)
            return
        barrier.wait(timeout=30)
        try:
            results.append(reserve(lg).session_id)
        except BaseException as e:          # noqa: BLE001
            errs.append(f"reserve: {type(e).__name__}: {e}")
        finally:
            lg.close()

    # daemon=True: on the hang path, a non-daemon thread wedged on a timeout-less barrier
    # keeps pytest alive forever (measured: 210s to fail, then the process never exits).
    threads = [threading.Thread(target=go, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(not t.is_alive() for t in threads), "a thread hung"
    assert len(results) + len(errs) == 8, "a thread vanished without a result or an error"
    assert errs == [], f"threads failed: {errs}"
    # `assert len(results) == 8` (rev 5 had it) is dropped: it follows from the two
    # assertions above (len(results) + len(errs) == 8, and errs == []), never fired under any
    # mutation, and a test line with zero detection power is noise pretending to be a guard.
    assert len(set(results)) == 1, f"the race minted {len(set(results))} sessions"


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


def test_commit_without_a_prior_assignment_is_refused(ledger):
    # The generation must be persisted BEFORE forwarding. A commit that invents one means
    # the request went somewhere the ledger never recorded.
    r = reserve(ledger)
    with pytest.raises(NelixError) as ei:
        ledger.commit(r.session_id, GID)
    assert ei.value.code == errors.IDEMPOTENCY_CONFLICT


def test_commit_cannot_rebind_an_assigned_generation_while_still_starting(ledger):
    # THE rev 2 hole: the guard only fired once state was already "started", so the window
    # assign_generation exists for was exactly the window it did not cover.
    r = reserve(ledger)
    ledger.assign_generation(r.session_id, GID)
    with pytest.raises(NelixError) as ei:
        ledger.commit(r.session_id, GID2)
    assert ei.value.code == errors.IDEMPOTENCY_CONFLICT
    assert reserve(ledger).generation_id == GID     # unchanged


def test_commit_rejects_a_malformed_generation(ledger):
    r = reserve(ledger)
    ledger.assign_generation(r.session_id, GID)
    with pytest.raises(NelixError) as ei:
        ledger.commit(r.session_id, "not-a-generation")
    assert ei.value.code == errors.INVALID_REQUEST


def test_failing_twice_with_the_same_reason_is_idempotent(ledger):
    r = reserve(ledger)
    ledger.fail(r.session_id, "bad cwd")
    ledger.fail(r.session_id, "bad cwd")
    assert reserve(ledger).reason == "bad cwd"


def test_failing_twice_with_a_different_reason_is_a_conflict(ledger):
    # A durable failure result must not be overwritten — a replay would report a reason the
    # caller never saw.
    r = reserve(ledger)
    ledger.fail(r.session_id, "bad cwd")
    with pytest.raises(NelixError) as ei:
        ledger.fail(r.session_id, "something else")
    assert ei.value.code == errors.IDEMPOTENCY_CONFLICT
    assert reserve(ledger).reason == "bad cwd"


@pytest.mark.parametrize("reason", [None, "", 42])
def test_fail_rejects_a_malformed_reason(ledger, reason):
    r = reserve(ledger)
    with pytest.raises(NelixError) as ei:
        ledger.fail(r.session_id, reason)
    assert ei.value.code == errors.INVALID_REQUEST


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


def test_a_start_that_already_acquired_a_session_cannot_be_failed(ledger, tmp_path):
    # Direction B, the mirror of create_session's guard. The router calls fail() on a forward
    # timeout — and cannot know whether the generation's create_session committed a moment
    # earlier. If fail() wins, the retry reports a durable failure while a worker is running,
    # and the caller dispatches a SECOND one.
    from nelix_store.store import Store
    store = Store(tmp_path, clock=lambda: 1000.0)
    try:
        r = reserve(ledger)
        ledger.assign_generation(r.session_id, GID)
        store.create_session(r.session_id, state="running", executor="coder", task="t",
                             cwd="/repo", model=None, created_at=100.0)
        with pytest.raises(NelixError) as ei:
            ledger.fail(r.session_id, "forward timed out")
        assert ei.value.code == errors.IDEMPOTENCY_CONFLICT
        # The start stays recoverable on its assigned generation, not poisoned.
        assert reserve(ledger).state == "starting"
    finally:
        store.close()


def test_a_session_and_a_failed_start_never_coexist_under_a_race(tmp_path):
    # The measured shape: 44/200 races left both. Whichever transaction wins, the OTHER must
    # refuse — BEGIN IMMEDIATE serializes them but does not decide the winner, so both sides
    # need a guard.
    from nelix_store.store import Store

    rounds, violations = 30, []
    for attempt in range(rounds):
        root = tmp_path / f"r{attempt}"
        lg = StartLedger(root, clock=lambda: 1000.0)
        store = Store(root, clock=lambda: 1000.0)
        try:
            r = lg.reserve(idempotency_key="k1", owner_id="hermes:local",
                           orchestration_id=OID, request_fingerprint=FP)
            lg.assign_generation(r.session_id, GID)
        finally:
            lg.close()
            store.close()

        barrier = threading.Barrier(2)

        def creator():
            s = Store(root, clock=lambda: 1000.0)
            barrier.wait(timeout=30)
            try:
                s.create_session(r.session_id, state="running", executor="coder", task="t",
                                 cwd="/repo", model=None, created_at=100.0)
            except NelixError:
                pass                       # losing is fine; coexisting is not
            finally:
                s.close()

        def failer():
            l2 = StartLedger(root, clock=lambda: 1000.0)
            barrier.wait(timeout=30)
            try:
                l2.fail(r.session_id, "forward timed out")
            except NelixError:
                pass
            finally:
                l2.close()

        threads = [threading.Thread(target=creator, daemon=True),
                   threading.Thread(target=failer, daemon=True)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert all(not t.is_alive() for t in threads), "a thread hung"

        check = StartLedger(root, clock=lambda: 1000.0)
        try:
            state = check._conn.execute(
                "SELECT state FROM starts WHERE session_id=?", (r.session_id,)
            ).fetchone()["state"]
            has_session = check._conn.execute(
                "SELECT 1 FROM sessions WHERE session_id=?", (r.session_id,)).fetchone()
        finally:
            check.close()
        if state == "failed" and has_session:
            violations.append(attempt)
    assert violations == [], (
        f"{len(violations)}/{rounds} races left a live session AND a failed start: {violations}")


def test_a_session_id_mint_collision_does_not_crash_reserve(tmp_path):
    # A minted-id collision raises IntegrityError on the PRIMARY KEY, not on the owner/key
    # UNIQUE — so rev 2's fall-through SELECT found no row and dereferenced None.
    collide = "s-" + "c" * 32
    lg = StartLedger(tmp_path, clock=lambda: 1000.0, mint=lambda: collide)
    lg.reserve(idempotency_key="k1", owner_id="hermes:local", orchestration_id=OID,
               request_fingerprint=FP)
    with pytest.raises(NelixError) as ei:      # a NelixError, never a raw TypeError
        lg.reserve(idempotency_key="k2", owner_id="hermes:local", orchestration_id=OID,
                   request_fingerprint=FP)
    assert ei.value.code in (errors.STORE_CORRUPT, errors.DUPLICATE_START)
    lg.close()
