"""The router's generation registry — ONE generation today, structurally multi-generation.

The registry owns the "active-generation pointer" (spec §1): new sessions route to the one
generation it tracks. It is deliberately LIST-shaped (a registry of N, N=1 today) so 3c.2/Plan 4
add generations without reshaping this — nothing hard-codes "one generation" in a way a later slice
must tear out.

Per generation it tracks:
  * `slot_id` — a STABLE per-slot `g-<32hex>` id (`new_generation_id()`), minted ONCE by the
    registry itself (analogous to how the router mints its own `router_epoch` once per process —
    see `router/app.py`) and reused for every incarnation this registry's one slot ever observes. It
    SURVIVES a daemon restart (unlike `epoch` below): it identifies the registry's generation SLOT,
    not any one incarnation of a daemon occupying it. This is the vector cursor's `generation_id`
    map KEY (`nelix_contracts.cursor`, `router/board.py`) — a caller's prior cursor position for
    this slot must not be orphaned by a daemon restart (nelix-3rm 3c.3a fix-pass finding #1).
  * `epoch` — a per-INCARNATION `generation_epoch` MINTED by the router (`new_generation_id()`, a
    g-<32hex> id — the exact shape the StartLedger stores and validates). The epoch is what
    assign_generation/commit bind a reservation to; keyed on the daemon's INCARNATION so a daemon
    RESTART yields a NEW epoch (spec §4: "a fresh generation epoch is minted on EVERY incarnation").
    The daemon's own /health carries no incarnation id yet (that addition is a later slice), so the
    incarnation is keyed on supervisor identity — (pid, start_fingerprint). Carried as the vector
    cursor's per-generation VALUE (paired with the seq), never its map key.
  * `transport` — the live daemon Transport. It is read TOGETHER with the incarnation UNDER THE
    LOCK from the AUTHORITATIVE lock holder (supervisor.held_generation()), so the epoch minted for
    an incarnation is never paired with a DIFFERENT incarnation's transport, and a snapshot captured
    outside the lock can never be installed stale over a newer incarnation. The incarnation is the
    daemon's VALIDATED LIVE SINGLETON-LOCK HOLDER — authoritative and monotonic where .active.json is
    not (a paused spawner can roll .active.json back to a superseded incarnation; a released lock
    cannot be re-held by the dead pid). Making a generation available (spawn + health check) happens
    OUTSIDE the lock first (ensure_running()).
  * `build_id` — the daemon's /health `generation_id` (may be None in dev — handled). Informational.

If a generation cannot be made available, callers get GENERATION_UNAVAILABLE (retryable)."""
import threading
from dataclasses import dataclass

from nelix_contracts.errors import GENERATION_UNAVAILABLE, NelixError
from nelix_contracts.ids import new_generation_id

import supervisor as _supervisor_module

# The owner the registry's /health probe constructs an RpcClient as. /health carries no owner on the
# wire (spec §8/§10); this value only satisfies RpcClient's construction check — it owns nothing.
# Mirrors supervisor._PROBE_OWNER's rationale.
PROBE_OWNER = "nelix-router-probe"


@dataclass(frozen=True)
class GenerationHandle:
    """A snapshot of one tracked generation. `slot_id` is the registry's STABLE per-slot id — minted
    once, survives a daemon restart, and is what the vector cursor keys on. `epoch` is the router's
    per-incarnation id (the ledger key; fresh on every restart). `transport` reaches the backend;
    `build_id` is the daemon's self-reported identity (or None); `incarnation` is the supervisor
    identity the epoch is keyed on."""
    slot_id: str
    epoch: str
    transport: object
    build_id: "str | None"
    incarnation: dict


def _default_health_probe(transport) -> "str | None":
    """Read a generation's build-id from GET /health, or None if it cannot be read (dev runtimes
    report None, and an unreachable/odd backend must not make the whole registry fail here — the
    build-id is informational; availability is decided by the transport, not by this probe)."""
    try:
        from rpc_client import RpcClient
    except ImportError:                                    # package mode
        from .rpc_client import RpcClient
    try:
        return RpcClient(transport, PROBE_OWNER).health().get("generation_id")
    except Exception:
        return None


class GenerationRegistry:
    """Thread-safe: the router shares ONE registry across its request threads. The epoch mint/refresh
    runs under a lock so two concurrent starts observing a fresh incarnation mint exactly ONE epoch
    (not two racing ids for the same generation); the slow steps (spawn, build-id probe) run outside
    that lock so a stalled generation never serializes all /start + /health behind them."""

    def __init__(self, *, supervisor=_supervisor_module, health_probe=_default_health_probe):
        self._sup = supervisor
        self._probe = health_probe
        self._lock = threading.Lock()
        # nelix-3rm 3c.3a fix-pass finding #1: the registry's ONE slot's STABLE id, minted exactly
        # once for the lifetime of this registry (analogous to router_epoch being minted once per
        # router process) -- it identifies the SLOT, never any one incarnation occupying it, so it
        # survives every daemon restart this registry ever observes.
        self._slot_id = new_generation_id()
        # The active-generation pointer, N=1: {"incarnation", "epoch", "build_id", "transport"} or
        # None before anything is observed. A dict, not the frozen handle, so the transport can be
        # refreshed in place while the epoch/build_id stay pinned to the incarnation.
        self._active = None
        # nelix-3rm 3c.3a: the vector cursor's topology component (nelix_contracts.cursor). N=1
        # today: the registry has exactly one slot and never adds/removes one, so this starts at a
        # fixed 1 and never moves yet -- but it is REAL mutable state (never a literal baked into a
        # cursor caller), so Plan 4's generation-add/remove surface has something to increment
        # under `self._lock` when the SET of tracked generations changes. A daemon restart mints a
        # fresh per-incarnation `epoch` (see `active()`) but is NOT a topology change -- the slot
        # count never moved -- so it must never bump this.
        self._topology_revision = 1

    def active(self) -> GenerationHandle:
        """Return the active generation, ensuring a backend is available. Raises
        NelixError(GENERATION_UNAVAILABLE) if none can be made available.

        The IDENTITY READ + epoch mint/install are ATOMIC under the lock (finding #1). The slow work
        stays OUTSIDE the lock: `_ensure_available()` (which may spawn a daemon and health-checks it)
        and the informational build-id probe (up to the health timeout). Then, UNDER the lock, the
        current `(transport, incarnation)` is read from the AUTHORITATIVE LIVE LOCK HOLDER
        (`supervisor.held_generation()` — a lock-file read + pid-liveness, no health RPC, no spawn) and
        the epoch is minted/installed together with it. Reading the identity under the lock — rather
        than carrying a snapshot captured OUTSIDE it — is what closes the race where a thread that
        observed incarnation A could install its STALE A over a newer B a concurrent thread already
        installed, rolling the active pointer backward and minting two epochs for one incarnation.

        The incarnation comes from the SINGLETON LOCK, not .active.json: the kernel guarantees exactly
        one live lock holder, so two concurrent callers read the SAME current incarnation (exactly one
        epoch) and `_active` can never roll backward to a superseded incarnation — a paused spawner
        that rewrites .active.json back to A cannot make the released lock be re-held by A's dead pid.
        The transport held_generation() returns is CONSISTENT with that same holder (re-derived from a
        unix holder's lock meta; for a tcp holder, paired with .active.json only when its incarnation
        matches — else the generation is reported unavailable/retryable rather than routed to a stale
        transport). If no authoritative holder is available, that is GENERATION_UNAVAILABLE."""
        self._ensure_available()                            # spawn / health-check — OUTSIDE the lock
        with self._lock:
            snap = self._sup.held_generation()              # authoritative identity+transport — no RPC
            if snap is None:
                raise NelixError(GENERATION_UNAVAILABLE,
                                 "generation disappeared before it could be pinned")
            transport, inc = snap
            if self._active is None or self._active["incarnation"] != inc:
                # First observation of THIS (current) incarnation -> mint exactly one fresh epoch and
                # install (epoch, transport, incarnation) together. build_id is filled by the unlocked
                # probe below (None until then; it is informational and nullable).
                self._active = {"incarnation": inc, "epoch": new_generation_id(),
                                "transport": transport, "build_id": None, "build_id_probed": False}
                need_probe = True
            else:
                self._active["transport"] = transport       # same incarnation: refresh the transport
                need_probe = not self._active["build_id_probed"]
            epoch = self._active["epoch"]
        build_id = None
        if need_probe:
            build_id = self._probe(transport)               # up to the health timeout — OUTSIDE the lock
            with self._lock:
                # Record it only if THIS incarnation is still active (a concurrent restart may have
                # replaced it) and no other thread already filled it.
                if (self._active is not None and self._active["incarnation"] == inc
                        and not self._active["build_id_probed"]):
                    self._active["build_id"] = build_id
                    self._active["build_id_probed"] = True
        else:
            with self._lock:
                if self._active is not None and self._active["incarnation"] == inc:
                    build_id = self._active["build_id"]
        # Pin the returned handle to the (epoch, transport, incarnation) validated together above.
        # slot_id is the registry's one stable slot id -- never re-minted, never per-incarnation.
        return GenerationHandle(slot_id=self._slot_id, epoch=epoch, transport=transport,
                                build_id=build_id, incarnation=inc)

    def topology_revision(self) -> int:
        """The vector cursor's topology component (nelix_contracts.cursor.new_cursor): bumped only
        when a generation is added to or removed from the registry (Plan 4). N=1 today, so this is
        a fixed 1 -- never a daemon-incarnation restart, which changes `epoch`, not the topology."""
        with self._lock:
            return self._topology_revision

    def generations(self, *, discover=False) -> list:
        """The registry as a LIST (N=1 today): the active-generation pointer, or []. List-shaped so
        3c.2/Plan 4 can return N generations without reshaping this interface.

        `discover=True` (nelix-3rm 3c.3a fix-pass finding #3 -- used ONLY by the fan-out board read,
        `router/board.py`): if nothing has been observed YET (`_active is None`), take one
        NON-SPAWNING discovery probe (`_discover_locked`, under the same lock `active()` uses)
        before answering `[]`. A router restart empties this registry but kills no daemon, so an
        unconditional `[]` would report an honestly-empty BOARD while a daemon already holds the
        singleton lock and serves live sessions the board must not hide. Only when that probe also
        finds nothing running is the board genuinely empty.

        The default (`discover=False` -- /health, /capabilities, /generation_list, unchanged) never
        takes this probe: those routes answer strictly from what this registry has already observed,
        exactly as before this fix-pass; widening THEIR contract is not this fix."""
        with self._lock:
            if self._active is None and discover:
                self._discover_locked()
            return [] if self._active is None else [self._handle(self._active)]

    def _discover_locked(self):
        """Must be called with `self._lock` held, and only when `self._active is None`. A single
        NON-SPAWNING probe of the CURRENT singleton-lock holder (`supervisor.held_generation()` — the
        same cheap lock-file read + pid-liveness `active()` uses UNDER ITS OWN LOCK; no /health RPC, no
        spawn). If a daemon currently holds the lock, install it as the active generation — minting its
        epoch under THIS SAME lock, so a concurrent `active()` call observing the identical incarnation
        never mints a second, racing epoch for it (the same one-epoch-per-incarnation invariant
        `active()` itself upholds). `slot_id` is never re-minted here: it is this registry's one
        already-existing stable slot id. If no daemon holds the lock, leave `_active` as None — the
        board really is empty, nothing to discover."""
        snap = self._sup.held_generation()
        if snap is None:
            return
        transport, inc = snap
        self._active = {"incarnation": inc, "epoch": new_generation_id(), "transport": transport,
                        "build_id": None, "build_id_probed": False}

    def _handle(self, a) -> GenerationHandle:
        return GenerationHandle(slot_id=self._slot_id, epoch=a["epoch"], transport=a["transport"],
                                build_id=a["build_id"], incarnation=a["incarnation"])

    def _ensure_available(self):
        """Make a HEALTHY generation available, OUTSIDE the lock (this is the slow work). Uses the
        full `active_generation()` read (which does the /health RPC); if nothing healthy is recorded,
        spawns one (ensure_running()) and re-checks. Returns nothing — the identity the caller pins is
        read separately UNDER the lock via `held_generation()` (the authoritative lock holder), so the
        epoch decision is atomic and never carries a stale outside-the-lock snapshot (finding #1). Any
        failure to make one available is GENERATION_UNAVAILABLE (retryable)."""
        try:
            if self._sup.active_generation() is None:
                # Nothing healthy recorded — spawn one, then re-check it became healthy.
                self._sup.ensure_running()
                if self._sup.active_generation() is None:
                    raise NelixError(GENERATION_UNAVAILABLE, "no generation backend available")
        except NelixError:
            raise
        except Exception as e:
            raise NelixError(GENERATION_UNAVAILABLE,
                             f"could not make a generation available: {e}") from None
