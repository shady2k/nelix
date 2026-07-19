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

    def held_generation(self):
        # The AUTHORITATIVE lock-holder read the registry does UNDER the lock to pin
        # identity+transport; here it mirrors active_generation() (no divergence between the lock
        # holder and .active.json in this fake — see the split-view fakes below for that).
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
    """The registry reads the transport and the incarnation TOGETHER (from held_generation(),
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

        def held_generation(self):
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


def test_topology_revision_starts_at_a_fixed_value():
    # nelix-3rm 3c.3a: N=1 today, so the registry has never added/removed a generation -- the
    # counter starts at a fixed value and the machinery (this getter) is what Plan 4 bumps when
    # a generation is added or removed, never a hardcoded literal baked into the cursor caller.
    reg = _reg(FakeSupervisor())
    assert reg.topology_revision() == 1


def test_topology_revision_is_unaffected_by_observing_or_restarting_a_generation():
    # A daemon RESTART mints a fresh per-incarnation epoch (test above), but that is not a
    # TOPOLOGY change -- the set of tracked generations (still just the one slot) never moved, so
    # topology_revision must not move either. Only Plan 4's add/remove-generation surface may
    # bump it.
    sup = FakeSupervisor(inc={"pid": 100, "start_fingerprint": "fp-1"})
    reg = _reg(sup)
    before = reg.topology_revision()
    reg.active()
    sup.inc = {"pid": 200, "start_fingerprint": "fp-2"}      # daemon restarted: new incarnation
    reg.active()
    assert reg.topology_revision() == before


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
    """Finding #1: the IDENTITY read + epoch mint are ATOMIC under the lock, and the identity comes
    from the AUTHORITATIVE LIVE LOCK HOLDER (held_generation()). A caller that did its (slow)
    availability ensure while incarnation A held the lock must read the CURRENT lock holder UNDER the
    lock — never install its STALE A over a newer B that a concurrent caller already installed.

    We drive the exact A -> B -> stale-A interleaving:
      * T1 begins its ensure while A holds the lock, then PARKS just before taking the epoch lock.
      * meanwhile the daemon restarts to B (B takes the released singleton lock) and a concurrent
        caller T2 fully installs B.
      * T1 resumes, takes the lock, and reads the CURRENT lock holder (B) — not its stale A.
    Asserted: exactly ONE epoch per incarnation, and the active pointer never rolls backward to A
    (a re-observation of B keeps B's single epoch; A never gets an epoch of its own)."""
    ta = Transport.unix("/tmp/gen-a.sock"); inc_a = {"pid": 1, "start_fingerprint": "fp-a"}
    tb = Transport.unix("/tmp/gen-b.sock"); inc_b = {"pid": 2, "start_fingerprint": "fp-b"}

    class SteppedSupervisor:
        def __init__(self):
            self._holder = (ta, inc_a)                     # the CURRENT singleton-lock holder
            self._pause_first_ensure = threading.Event()   # arm: the next ensure captures A then parks
            self._ensured = threading.Event()              # set once the paused caller has ensured
            self._release = threading.Event()              # the test releases the paused caller

        def restart_to_b(self):
            self._holder = (tb, inc_b)                     # B acquires the released singleton lock

        def active_generation(self):                       # OUTSIDE the lock (the slow ensure)
            captured = self._holder                        # what THIS caller observed (era A for T1)
            if self._pause_first_ensure.is_set():
                self._pause_first_ensure.clear()           # only the first caller parks
                self._ensured.set()
                self._release.wait(2)
            return captured

        def held_generation(self):                         # UNDER the lock (authoritative identity)
            return self._holder                            # always the CURRENT live lock holder

        def ensure_running(self):
            return self._holder[0]

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


def test_lock_holder_is_authoritative_over_a_rolled_back_active_json():
    """Finding #1 (rev 3): the under-lock identity is the VALIDATED LIVE LOCK HOLDER
    (held_generation()), NOT .active.json — which can ROLL BACK. Interleaving: an ensure_running
    thread validates daemon A and pauses before _write_state(A); a teardown kills A; daemon B takes
    the released singleton lock and publishes B; the paused thread resumes and writes A over B in
    .active.json. A bare .active.json read then reports the superseded A (A's zombie still passes a
    bare pid-liveness check) — a ROLLBACK that misroutes a start to A's dead transport and mints a
    SECOND epoch for B.

    Modeled by a supervisor whose .active.json VIEW (active_generation) can regress to A while its
    singleton-LOCK HOLDER (held_generation) stays B. The registry MUST resolve to B: never install or
    route A, never roll the active pointer back to A, and mint exactly ONE epoch for B."""
    ta = Transport.unix("/tmp/gen-a.sock"); inc_a = {"pid": 1, "start_fingerprint": "fp-a"}
    tb = Transport.unix("/tmp/gen-b.sock"); inc_b = {"pid": 2, "start_fingerprint": "fp-b"}

    class SplitSupervisor:
        """The .active.json view (active_generation) can DIVERGE from the singleton lock holder
        (held_generation); the lock holder is the authoritative, monotonic incarnation."""
        def __init__(self):
            self.active_view = (tb, inc_b)     # what .active.json reports (the outside-the-lock ensure)
            self.holder = (tb, inc_b)          # what the singleton lock holder reports (authoritative)

        def active_generation(self):
            return self.active_view

        def held_generation(self):
            return self.holder

        def ensure_running(self):
            return self.holder[0]

    sup = SplitSupervisor()
    reg = GenerationRegistry(supervisor=sup, health_probe=lambda t: None)
    g_b = reg.active()                                      # B observed first: mint B's single epoch
    assert g_b.incarnation == inc_b and g_b.transport == tb

    # .active.json ROLLS BACK to the superseded incarnation A (the paused spawner's late write), but
    # the singleton lock is STILL held by B. The registry pins identity from the LOCK HOLDER.
    sup.active_view = (ta, inc_a)
    g_again = reg.active()
    assert g_again.incarnation == inc_b                     # never rolled back to A
    assert g_again.transport == tb                          # routed to B's transport, not the stale A
    assert g_again.epoch == g_b.epoch                       # exactly ONE epoch for B; A minted none
    gens = reg.generations()
    assert len(gens) == 1 and gens[0].incarnation == inc_b  # active pointer still B


# ================================================= nelix-3rm 3c.3a fix-pass finding #1: slot_id

def test_slot_id_is_stable_across_a_restart_while_epoch_is_not():
    # The cursor's map KEY must be the STABLE slot id, not the volatile per-incarnation epoch --
    # a daemon restart must never orphan a caller's prior cursor position for this generation.
    sup = FakeSupervisor(inc={"pid": 100, "start_fingerprint": "fp-1"})
    reg = _reg(sup)
    first = reg.active()
    sup.inc = {"pid": 200, "start_fingerprint": "fp-2"}     # daemon restarted: new incarnation
    second = reg.active()
    assert first.epoch != second.epoch                     # epoch: per-incarnation, as before
    assert first.generation_id == second.generation_id                  # slot_id: stable across the restart
    assert _EPOCH_RE.match(first.generation_id)                   # same g-<32hex> shape as epoch


def test_slot_id_is_stable_across_repeated_observations_of_the_same_incarnation():
    reg = _reg(FakeSupervisor())
    assert reg.active().generation_id == reg.active().generation_id


def test_slot_id_is_never_the_nullable_build_id():
    # The fix explicitly forbids reusing build_id (nullable, and not validate_generation_id-shaped
    # in dev where the health probe reports None) as the cursor's stable key.
    gen = GenerationRegistry(supervisor=FakeSupervisor(), health_probe=lambda t: None).active()
    assert gen.build_id is None
    assert gen.generation_id is not None
    assert _EPOCH_RE.match(gen.generation_id)


def test_generations_reports_the_same_slot_id_as_active():
    sup = FakeSupervisor()
    reg = _reg(sup)
    pinned = reg.active()
    listed = reg.generations()[0]
    assert listed.generation_id == pinned.generation_id


# ============================================ nelix-3rm 3c.3a fix-pass finding #3: discovery

def test_generations_default_never_discovers_a_daemon_it_has_not_observed():
    # The default (discover=False -- /health, /capabilities, /generation_list) is UNCHANGED by this
    # fix-pass: an empty registry stays honestly [] unless a caller explicitly opts into discovery.
    sup = FakeSupervisor()                                  # held_generation() would report a live one
    reg = _reg(sup)
    assert reg.generations() == []


def test_generations_discover_true_discovers_a_currently_held_incarnation_when_empty():
    # nelix-3rm 3c.3a fix-pass finding #3: a router restart empties the registry but kills no
    # daemon -- discover=True finds the daemon already holding the singleton lock via the same
    # non-spawning held_generation() read active() uses under its own lock.
    sup = FakeSupervisor()                                  # recorded=True: a daemon IS currently held
    reg = _reg(sup)
    gens = reg.generations(discover=True)
    assert len(gens) == 1
    g = gens[0]
    assert g.incarnation == sup.inc
    assert g.transport == sup.transport
    assert _EPOCH_RE.match(g.epoch)
    assert _EPOCH_RE.match(g.generation_id)


def test_generations_discover_true_is_honestly_empty_when_no_daemon_is_held():
    sup = FakeSupervisor()
    sup.recorded = False                                    # no live incarnation to discover
    reg = _reg(sup)
    assert reg.generations(discover=True) == []


def test_generations_discover_true_never_calls_the_spawning_ensure_path():
    class _BoomIfSpawned:
        def __init__(self, transport, inc):
            self._t, self._inc = transport, inc

        def held_generation(self):
            return (self._t, self._inc)

        def active_generation(self):
            raise AssertionError("discovery must never take the spawning ensure path")

        def ensure_running(self):
            raise AssertionError("discovery must never spawn a generation")

    sup = _BoomIfSpawned(Transport.unix("/tmp/discover.sock"),
                         {"pid": 9, "start_fingerprint": "fp-9"})
    reg = GenerationRegistry(supervisor=sup, health_probe=lambda t: None)
    gens = reg.generations(discover=True)
    assert len(gens) == 1 and gens[0].incarnation == sup._inc


def test_generations_discover_true_installs_the_same_epoch_and_slot_id_active_then_returns():
    # Discovery must mint exactly ONE epoch for the discovered incarnation: a subsequent active()
    # call for the SAME incarnation must reuse it, never mint a second, racing one.
    sup = FakeSupervisor()
    reg = _reg(sup)
    discovered = reg.generations(discover=True)[0]
    pinned = reg.active()
    assert discovered.epoch == pinned.epoch
    assert discovered.generation_id == pinned.generation_id
    assert discovered.incarnation == pinned.incarnation


def test_generations_discover_true_is_idempotent_across_repeated_calls():
    sup = FakeSupervisor()
    reg = _reg(sup)
    first = reg.generations(discover=True)[0]
    second = reg.generations(discover=True)[0]
    assert first.epoch == second.epoch and first.generation_id == second.generation_id
