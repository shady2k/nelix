"""nelix-3rm slice 3c.1 Part C: the generation registry — ONE generation today, structurally
multi-generation (list-shaped, N=1). It ensures a generation backend is available (supervisor
endpoint, else ensure_running), and mints a per-INCARNATION generation EPOCH keyed on the daemon's
process identity so a daemon RESTART yields a NEW epoch (spec §4). The epoch is a g-<32hex> id — the
shape the StartLedger stores and validates."""
import re
import threading

import pytest

from nelix_contracts.errors import GENERATION_UNAVAILABLE, NelixError
from daemon.transport import Transport
from router.registry import GenerationRegistry

_EPOCH_RE = re.compile(r"^g-[0-9a-f]{32}$")


class FakeSupervisor:
    def __init__(self, transport=None, inc=None):
        self.transport = transport or Transport.unix("/tmp/fake-gen.sock")
        self.inc = inc if inc is not None else {"pid": 100, "start_fingerprint": "fp-1"}
        self.ensure_calls = 0
        # Whether a live, healthy generation is currently recorded. Set False to model "no daemon
        # yet"; ensure_running() (a spawn) publishes one.
        self.recorded = True

    def active_generation(self):
        # transport + incarnation ALWAYS returned as one pair (the atomic snapshot the registry
        # consumes) — or None when nothing live/healthy is recorded.
        if not self.recorded or self.inc is None:
            return None
        return (self.transport, self.inc)

    def ensure_running(self):
        self.ensure_calls += 1
        self.recorded = True                                   # a spawn publishes a live generation
        return self.transport


def _reg(sup, build_id="build-xyz"):
    return GenerationRegistry(supervisor=sup, health_probe=lambda t: build_id)


def test_active_returns_a_minted_epoch_transport_and_build_id():
    sup = FakeSupervisor()
    gen = _reg(sup).active()
    assert _EPOCH_RE.match(gen.epoch)
    assert gen.transport == sup.transport
    assert gen.build_id == "build-xyz"


def test_same_incarnation_keeps_the_same_epoch():
    reg = _reg(FakeSupervisor())
    assert reg.active().epoch == reg.active().epoch


def test_a_restart_new_incarnation_mints_a_fresh_epoch():
    sup = FakeSupervisor(inc={"pid": 100, "start_fingerprint": "fp-1"})
    reg = _reg(sup)
    first = reg.active().epoch
    sup.inc = {"pid": 200, "start_fingerprint": "fp-2"}   # daemon restarted: new pid+fingerprint
    second = reg.active().epoch
    assert first != second
    assert _EPOCH_RE.match(second)


def test_epoch_and_transport_are_paired_from_one_snapshot_across_a_restart():
    """Finding #3: the registry must read the transport and the incarnation TOGETHER, so a new
    incarnation's epoch is never paired with a prior incarnation's transport. The stub here ONLY
    exposes active_generation() (no separate endpoint()/incarnation()), so a registry that tried the
    old separate reads would AttributeError — proving the pairing is structural, not incidental. A
    restart swaps BOTH transport and incarnation in one snapshot; the new epoch must arrive with the
    NEW transport."""
    class ConsistentSupervisor:
        def __init__(self):
            # transport and incarnation are always read as ONE pair.
            self.snapshot = (Transport.unix("/tmp/gen-a.sock"),
                             {"pid": 1, "start_fingerprint": "fp-a"})

        def active_generation(self):
            return self.snapshot

        def ensure_running(self):
            return self.snapshot[0] if self.snapshot else None

    sup = ConsistentSupervisor()
    reg = GenerationRegistry(supervisor=sup, health_probe=lambda t: None)
    g1 = reg.active()
    assert g1.transport == sup.snapshot[0]
    assert g1.incarnation == sup.snapshot[1]

    # Daemon restart: the snapshot swaps transport AND incarnation together.
    sup.snapshot = (Transport.unix("/tmp/gen-b.sock"), {"pid": 2, "start_fingerprint": "fp-b"})
    g2 = reg.active()
    assert g2.epoch != g1.epoch                            # a fresh incarnation minted a fresh epoch
    assert g2.transport == sup.snapshot[0]                 # ...paired with the NEW transport,
    assert g2.transport != g1.transport                    #    never the stale one
    assert _EPOCH_RE.match(g2.epoch)


def test_no_recorded_generation_falls_back_to_ensure_running():
    sup = FakeSupervisor()
    sup.recorded = False                                   # nothing live/healthy recorded yet
    gen = _reg(sup).active()
    assert sup.ensure_calls == 1                           # spawned one, then re-read the snapshot
    assert gen.transport == sup.transport


def test_generation_unavailable_when_backend_cannot_be_made_available():
    sup = FakeSupervisor()
    sup.recorded = False

    def _boom():
        raise RuntimeError("daemon did not become healthy")
    sup.ensure_running = _boom
    with pytest.raises(NelixError) as ei:
        _reg(sup).active()
    assert ei.value.code == GENERATION_UNAVAILABLE
    assert ei.value.retryable is True


def test_missing_incarnation_is_generation_unavailable():
    sup = FakeSupervisor()
    sup.inc = None                                         # transport up but no incarnation (race)
    with pytest.raises(NelixError) as ei:
        _reg(sup).active()
    assert ei.value.code == GENERATION_UNAVAILABLE


def test_build_id_may_be_null_in_dev():
    gen = GenerationRegistry(supervisor=FakeSupervisor(),
                             health_probe=lambda t: None).active()
    assert gen.build_id is None


def test_registry_is_list_shaped_n_equals_one():
    reg = _reg(FakeSupervisor())
    assert reg.generations() == []                         # nothing observed yet
    gen = reg.active()
    gens = reg.generations()
    assert len(gens) == 1 and gens[0].epoch == gen.epoch


def test_concurrent_active_on_a_fresh_incarnation_mints_one_epoch():
    reg = _reg(FakeSupervisor())
    seen = []
    barrier = threading.Barrier(8)

    def _go():
        barrier.wait()
        seen.append(reg.active().epoch)

    threads = [threading.Thread(target=_go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(seen)) == 1                             # exactly ONE epoch across all threads
