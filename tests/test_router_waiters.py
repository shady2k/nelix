"""The waiter registry: the router's own count of attached orchestration long-polls, and its
appearance in the board. This is what later lets a host tell 'someone is listening' from 'nobody
is' without guessing from process tables."""
import threading

from router.waiters import WaiterRegistry

ORCH = "o-" + "1" * 32


def test_count_is_zero_before_anyone_attaches():
    assert WaiterRegistry().count(ORCH) == 0


def test_a_waiter_is_counted_while_attached_and_released_after():
    reg = WaiterRegistry()

    with reg.attached(ORCH):
        assert reg.count(ORCH) == 1

    assert reg.count(ORCH) == 0


def test_concurrent_waiters_are_counted_independently():
    reg = WaiterRegistry()
    inside = threading.Event()
    release = threading.Event()

    def hold():
        with reg.attached(ORCH):
            inside.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    inside.wait(timeout=5)
    with reg.attached(ORCH):
        assert reg.count(ORCH) == 2
    release.set()
    t.join(timeout=5)

    assert reg.count(ORCH) == 0


def test_an_exception_inside_the_block_still_releases_the_slot():
    reg = WaiterRegistry()

    try:
        with reg.attached(ORCH):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert reg.count(ORCH) == 0


def test_counts_reports_only_orchestrations_with_live_waiters():
    reg = WaiterRegistry()

    with reg.attached(ORCH):
        assert reg.counts() == {ORCH: 1}

    assert reg.counts() == {}


import pytest

from nelix_store.ledger import StartLedger

OWNER = "harness-x"
OTHER_ORCH = "o-" + "2" * 32


@pytest.fixture
def ledger(tmp_path):
    led = StartLedger(tmp_path)
    try:
        yield led
    finally:
        led.close()


def test_orchestrations_for_owner_is_empty_when_nothing_started(ledger):
    assert ledger.orchestrations_for_owner(OWNER) == []


def test_orchestrations_for_owner_lists_each_orchestration_once(ledger):
    for i, orch in enumerate((ORCH, ORCH, OTHER_ORCH)):
        ledger.reserve(idempotency_key=f"k{i}", owner_id=OWNER, orchestration_id=orch,
                       request_fingerprint=f"fp{i}")

    assert sorted(ledger.orchestrations_for_owner(OWNER)) == sorted([ORCH, OTHER_ORCH])


def test_orchestrations_for_owner_is_owner_scoped(ledger):
    ledger.reserve(idempotency_key="k", owner_id=OWNER, orchestration_id=ORCH,
                   request_fingerprint="fp")

    assert ledger.orchestrations_for_owner("harness-y") == []


def test_the_board_reports_waiters_for_a_live_orchestration(real_router, capsys):
    """A real router, a real board read: with nothing attached the orchestration map is empty,
    and it never crashes when no orchestration exists at all.

    Read through `nelix daemon status`, which is the CLI verb this checkout has for the router's
    GET /status board route. The fact under test is router-side — that the board carries the new
    `orchestrations` key — so it does not depend on which CLI verb spells the read."""
    import json

    import nelix_cli

    assert nelix_cli.main(["daemon", "ensure"]) == 0
    capsys.readouterr()

    assert nelix_cli.main(["daemon", "status", "--owner", OWNER]) == 0
    body = json.loads(capsys.readouterr().out)

    assert body["orchestrations"] == {}


# ---------------------------------------------------------------------------------------------
# The WIRING. Everything above proves a HALF: the registry counts correctly in isolation, and the
# board carries the key. Neither proves the two are CONNECTED — the number the board prints has to
# be the long-poll WaitForward is holding at that instant, or the fact is worthless.
# ---------------------------------------------------------------------------------------------

from nelix_contracts.cursor import encode, new_cursor
from router.board import BoardForward
from router.registry import GenerationRegistry
from router.wait import WaitForward

import paths
from tests._router_fakes import Backend, Supervisor

EPOCH = "r-" + "0" * 32
AE = 42


class _ParkedBoardSeqStore:
    """A Store duck whose board_seq read PARKS its caller until the test releases it.

    The park point sits inside WaitForward's archive multiplex loop — that is, inside the counted
    region — so the test can read the board while a long-poll is genuinely in flight, with no sleep
    and no guessing at a window. Releasing returns a seq ABOVE the cursor's armed position, which
    is exactly what ends the long-poll (an archive wake), so the test never waits out a timeout."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def get_owner_board_seq(self, owner_id):
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise AssertionError("the parked long-poll was never released")
        return 1


def test_the_board_reports_the_long_poll_wait_forward_is_actually_holding():
    """ONE shared WaiterRegistry across both collaborators — exactly how make_router_server builds
    them — with a real waitable session so wait() long-polls instead of short-circuiting on
    empty_orchestration.

    This is the fact the Stop-hook interlock stands on: it reads `waiters` to tell "live sessions
    nobody is listening to" from "someone is on it". Drop the counting wrapper in WaitForward.wait
    and the board reports waiters: 0 forever while a waiter is plainly attached — the interlock
    would then block every turn. So this test asserts the count DURING the blocking call, which is
    the only moment at which the wiring can be observed at all."""
    backend = Backend()
    sup = Supervisor(backend.transport)
    registry = GenerationRegistry(supervisor=sup, health_probe=lambda t: backend.build_id)
    ledger = StartLedger(paths.nelix_root())
    store = _ParkedBoardSeqStore()
    waiters = WaiterRegistry()                  # ONE instance, handed to BOTH — the wiring itself
    board = BoardForward(registry, EPOCH, ledger=ledger, waiters=waiters)
    wait = WaitForward(ledger, registry, EPOCH, store=store, archive_epoch=AE, waiters=waiters)

    try:
        sid = ledger.reserve(idempotency_key="k-wiring", owner_id=OWNER, orchestration_id=ORCH,
                             request_fingerprint="fp").session_id
        backend.owns[sid] = OWNER
        # An armed archive component, so the multiplex loop's FIRST board_seq read is the park
        # point (a cursor without one would spend a read on the "start from now" arm instead).
        token = encode(new_cursor(EPOCH, registry.topology_revision()).advance_archive(AE, 0))

        reply = {}

        def _hold():
            reply["result"] = wait.wait(OWNER, ORCH, token)

        t = threading.Thread(target=_hold)
        t.start()
        try:
            assert store.entered.wait(timeout=10), "wait() never reached its long-poll"

            # THE ASSERTION: a different caller, on a different thread, reading the board through
            # the ordinary route sees the long-poll that is in flight right now.
            _status, body = board.status(OWNER)
            assert body["orchestrations"][ORCH] == {"sessions": 1, "waiters": 1}
        finally:
            store.release.set()
            t.join(timeout=10)
        assert not t.is_alive()

        # It really was a BLOCKING long-poll that woke, not an early return that never counted:
        # a short-circuit would have made the assertion above vacuously true of a different call.
        status, resp = reply["result"]
        assert status == 200
        assert resp["event"] == {"kind": "archive"}

        # ...and the slot is handed back once the poll returns.
        _status, body = board.status(OWNER)
        assert body["orchestrations"][ORCH] == {"sessions": 1, "waiters": 0}
    finally:
        ledger.close()
        backend.close()
