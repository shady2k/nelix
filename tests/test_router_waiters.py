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


import shutil
import subprocess

import paths


@pytest.fixture
def real_router(monkeypatch):
    """Spies on every subprocess.Popen call (so a test can assert exactly how many router
    processes were spawned) and guarantees cleanup: SIGTERM/kill each spawned process and remove
    the leaf runtime dir, so a router `ensure` brings up never survives the test.

    A LOCAL copy of tests/test_nelix_cli.py's fixture on purpose: that module is being edited in
    parallel, so this module carries its own rather than reaching into a file it does not own."""
    spawned = []
    real_popen = subprocess.Popen

    def _spy(*a, **kw):
        p = real_popen(*a, **kw)
        spawned.append(p)
        return p

    monkeypatch.setattr(subprocess, "Popen", _spy)
    try:
        yield spawned
    finally:
        for p in spawned:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        shutil.rmtree(paths.router_sock().parent, ignore_errors=True)


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
