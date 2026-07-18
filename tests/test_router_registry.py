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
        # The FULL (health-checked) read the registry does OUTSIDE the lock to ensure availability —
        # or None when nothing live/healthy is recorded.
        if not self.recorded or self.inc is None:
            return None
        return (self.transport, self.inc)

    def current_generation(self):
        # The CHEAP read the registry does UNDER the lock to pin identity+transport; here it mirrors
        # active_generation() (there is no separate health probe in this fake).
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
    """The registry reads the transport and the incarnation TOGETHER (from current_generation(),
    UNDER the lock), so a new incarnation's epoch is never paired with a prior incarnation's
    transport. A restart swaps BOTH transport and incarnation in one snapshot; the new epoch must
    arrive with the NEW transport."""
    class ConsistentSupervisor:
        def __init__(self):
            # transport and incarnation are always read as ONE pair.
            self.snapshot = (Transport.unix("/tmp/gen-a.sock"),
                             {"pid": 1, "start_fingerprint": "fp-a"})

        def active_generation(self):
            return self.snapshot

        def current_generation(self):
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


def test_stale_snapshot_never_installs_over_a_newer_incarnation():
    """Finding #1: the IDENTITY read + epoch mint are ATOMIC under the lock. A caller that did its
    (slow) availability ensure while incarnation A was live must read the CURRENT incarnation UNDER
    the lock — never install its STALE A over a newer B that a concurrent caller already installed.

    We drive the exact A -> B -> stale-A interleaving:
      * T1 begins its ensure while A is live, then PARKS just before taking the epoch lock.
      * meanwhile the daemon restarts to B and a concurrent caller T2 fully installs B.
      * T1 resumes, takes the lock, and reads the CURRENT incarnation (B) — not its stale A.
    Asserted: exactly ONE epoch per incarnation, and the active pointer never rolls backward to A
    (a re-observation of B keeps B's single epoch; A never gets an epoch of its own)."""
    ta = Transport.unix("/tmp/gen-a.sock"); inc_a = {"pid": 1, "start_fingerprint": "fp-a"}
    tb = Transport.unix("/tmp/gen-b.sock"); inc_b = {"pid": 2, "start_fingerprint": "fp-b"}

    class SteppedSupervisor:
        def __init__(self):
            self._pair = (ta, inc_a)                       # the CURRENT recorded generation
            self._pause_first_ensure = threading.Event()   # arm: the next ensure captures A then parks
            self._ensured = threading.Event()              # set once the paused caller has ensured
            self._release = threading.Event()              # the test releases the paused caller

        def restart_to_b(self):
            self._pair = (tb, inc_b)

        def active_generation(self):                       # OUTSIDE the lock (the slow ensure)
            captured = self._pair                          # what THIS caller observed (era A for T1)
            if self._pause_first_ensure.is_set():
                self._pause_first_ensure.clear()           # only the first caller parks
                self._ensured.set()
                self._release.wait(2)
            return captured

        def current_generation(self):                      # UNDER the lock (cheap identity read)
            return self._pair                              # always the CURRENT recorded generation

        def ensure_running(self):
            return self._pair[0]

    sup = SteppedSupervisor()
    reg = GenerationRegistry(supervisor=sup, health_probe=lambda t: None)

    out = {}
    sup._pause_first_ensure.set()
    t1 = threading.Thread(target=lambda: out.__setitem__("g1", reg.active()), name="T1")
    t1.start()
    assert sup._ensured.wait(2)                            # T1 ensured on era A and parked pre-lock

    sup.restart_to_b()                                     # daemon restarts to incarnation B
    g2 = reg.active()                                      # T2: ensure(B) -> lock -> mint+install B
    assert g2.incarnation == inc_b

    sup._release.set()                                     # release T1
    t1.join(2)
    g1 = out["g1"]

    assert g1.incarnation == inc_b                         # under-lock read gave B, not the stale A
    assert g1.epoch == g2.epoch                            # exactly ONE epoch for incarnation B
    g3 = reg.active()                                      # no rollback: still B, same single epoch
    assert g3.incarnation == inc_b and g3.epoch == g2.epoch
    gens = reg.generations()
    assert len(gens) == 1 and gens[0].incarnation == inc_b
