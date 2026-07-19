"""The router's generation registry — ONE generation today, structurally multi-generation.

The registry owns the "active-generation pointer" (spec §1): new sessions route to the one
generation it tracks. It is deliberately LIST-shaped (a registry of N, N=1 today) so 3c.2/Plan 4
add generations without reshaping this — nothing hard-codes "one generation" in a way a later slice
must tear out.

Per generation it tracks:
  * `generation_id` — a STABLE `g-<32hex>` id (`new_generation_id()`), minted ONCE by the
    registry itself (analogous to how the router mints its own `router_epoch` once per process —
    see `router/app.py`) and PERSISTED via `store.create_generation(...)`. It SURVIVES a daemon
    restart (unlike `epoch` below): it identifies the registry's generation SLOT, not any one
    incarnation of a daemon occupying it. This is the vector cursor's `generation_id` map KEY
    (`nelix_contracts.cursor`, `router/board.py`) — a caller's prior cursor position for this slot
    must not be orphaned by a daemon restart.
  * `epoch` — a per-INCARNATION `generation_epoch` MINTED by the router (`new_generation_id()`, a
    g-<32hex> id — the exact shape the StartLedger stores and validates). The epoch is what
    assign_generation/commit bind a reservation to; keyed on the daemon's INCARNATION so a daemon
    RESTART yields a NEW epoch. The daemon's own /health carries no incarnation id yet (that
    addition is a later slice), so the incarnation is keyed on supervisor identity —
    (pid, start_fingerprint). Carried as the vector cursor's per-generation VALUE (paired with the
    seq), never its map key.
  * `transport` — the live daemon Transport.
  * `build_id` — the daemon's /health `generation_id` (may be None in dev — handled). Informational.

If a generation cannot be made available, callers get GENERATION_UNAVAILABLE (retryable).
"""
import json
import threading
import time
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
    """A snapshot of one tracked generation. `generation_id` is the durable STABLE id — persisted
    via store.create_generation(), survives a daemon restart, and is what the vector cursor keys
    on. `epoch` is the router's per-incarnation id (the ledger key; fresh on every restart).
    `transport` reaches the backend; `build_id` is the daemon's self-reported identity (or None);
    `incarnation` is the supervisor identity the epoch is keyed on."""
    generation_id: str
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


def _incarnation_meta(inc: dict) -> str:
    """Deterministic canonical JSON of the incarnation identity dict."""
    return json.dumps(inc, sort_keys=True)


class GenerationRegistry:
    """Thread-safe: the router shares ONE registry across its request threads. The epoch mint/refresh
    runs under a lock so two concurrent starts observing a fresh incarnation mint exactly ONE epoch
    (not two racing ids for the same generation); the slow steps (spawn, build-id probe) run outside
    that lock so a stalled generation never serializes all /start + /health behind them."""

    def __init__(self, *, store=None, supervisor=_supervisor_module,
                 health_probe=_default_health_probe):
        self._store = store
        self._sup = supervisor
        self._probe = health_probe
        self._lock = threading.Lock()
        # The registry's ONE slot's STABLE generation id, minted exactly once for the lifetime of
        # this registry — it identifies the SLOT, never any one incarnation occupying it, so it
        # survives every daemon restart this registry ever observes. Persisted via
        # store.create_generation(...) on first observation.
        self._generation_id = new_generation_id()
        self._generation_created = False
        # The active-generation pointer, N=1: {"incarnation", "epoch", "generation_id",
        # "build_id", "transport"} or None before anything is observed.
        self._active = None
        # The vector cursor's topology component. N=1 today: the registry has exactly one slot and
        # never adds/removes one, so this starts at a fixed 1 and never moves yet.
        self._topology_revision = 1

    def active(self) -> GenerationHandle:
        """Return the active generation, ensuring a backend is available. Raises
        NelixError(GENERATION_UNAVAILABLE) if none can be made available.

        The IDENTITY READ + epoch mint/install are ATOMIC under the lock. The slow work stays
        OUTSIDE the lock: `_ensure_available()` (which may spawn a daemon and health-checks it)
        and the informational build-id probe (up to the health timeout). Then, UNDER the lock, the
        current `(transport, incarnation)` is read from the AUTHORITATIVE LIVE LOCK HOLDER
        (`supervisor.held_generation()`) and the epoch is minted/installed together with it.

        Per S1b, the registry now persists the generation identity:
          - First observation: store.create_generation() with the stable generation_id.
          - Per incarnation: store.insert_epoch(starting) -> health -> cas_epoch_serving.
          - On incarnation change: mark old epoch dead before inserting new starting epoch.
        """
        self._ensure_available()                            # spawn / health-check — OUTSIDE the lock
        with self._lock:
            snap = self._sup.held_generation()              # authoritative identity+transport — no RPC
            if snap is None:
                raise NelixError(GENERATION_UNAVAILABLE,
                                 "generation disappeared before it could be pinned")
            transport, inc = snap

            if self._active is None:
                # First observation of ANY incarnation: persist the stable generation and
                # the first epoch, then CAS to serving.
                return self._first_observation(transport, inc)

            if self._active["incarnation"] != inc:
                # Daemon respawn: new incarnation under the same generation_id.
                return self._new_incarnation(transport, inc)

            # Same incarnation: refresh the transport, re-use the existing epoch.
            self._active["transport"] = transport
            epoch = self._active["epoch"]
            generation_id = self._active["generation_id"]
            build_id = self._active.get("build_id")
            return GenerationHandle(generation_id=generation_id, epoch=epoch,
                                    transport=transport, build_id=build_id, incarnation=inc)

    def _first_observation(self, transport, inc) -> GenerationHandle:
        """First observation of any daemon incarnation. Create the generation row, insert the
        first starting epoch, then CAS to serving. On any failure, mark the epoch dead."""
        clock = time.time()
        gid = self._generation_id

        # Resolve build_id for the validated incarnation before create_generation.
        build_id = self._probe(transport)
        if self._store is not None:
            try:
                self._store.create_generation(gid, build_id=build_id, lifecycle_state="active",
                                              capability_snapshot=None, created_at=clock)
            except Exception:
                raise NelixError(GENERATION_UNAVAILABLE,
                                 "failed to persist the generation identity")

        epoch = new_generation_id()
        meta = _incarnation_meta(inc)
        if self._store is not None:
            try:
                self._store.insert_epoch(epoch, gid, incarnation_meta=meta, created_at=clock)
            except Exception:
                raise NelixError(GENERATION_UNAVAILABLE,
                                 "failed to persist the starting epoch")

        if self._store is not None:
            # Health-check already done by _ensure_available; CAS promote.
            try:
                self._store.cas_epoch_serving(gid, epoch, expected_current_epoch=None)
            except Exception:
                self._store.set_epoch_process_state(epoch, "dead")
                raise NelixError(GENERATION_UNAVAILABLE,
                                 "epoch promotion failed after health check")

        self._active = {
            "incarnation": inc, "epoch": epoch, "generation_id": gid,
            "transport": transport, "build_id": build_id, "build_id_probed": True,
        }
        return GenerationHandle(generation_id=gid, epoch=epoch, transport=transport,
                                build_id=build_id, incarnation=inc)

    def _new_incarnation(self, transport, inc) -> GenerationHandle:
        """Daemon respawn: mark the old epoch dead, insert a new starting epoch, then CAS to
        serving. On any failure, mark the new epoch dead."""
        gid = self._active["generation_id"]
        old_epoch = self._active["epoch"]

        if self._store is not None:
            # Mark old epoch dead BEFORE inserting the new one (the partial-unique serving index
            # forbids two serving epochs).
            try:
                self._store.set_epoch_process_state(old_epoch, "dead")
            except Exception:
                # Old epoch already dead or gone — continue; the new epoch must still be created.
                pass

        clock = time.time()
        epoch = new_generation_id()
        meta = _incarnation_meta(inc)
        if self._store is not None:
            try:
                self._store.insert_epoch(epoch, gid, incarnation_meta=meta, created_at=clock)
            except Exception:
                raise NelixError(GENERATION_UNAVAILABLE,
                                 "failed to persist the starting epoch for new incarnation")

        if self._store is not None:
            # Health-check already done by _ensure_available; CAS promote with expected=old_epoch.
            try:
                self._store.cas_epoch_serving(gid, epoch, expected_current_epoch=old_epoch)
            except Exception:
                self._store.set_epoch_process_state(epoch, "dead")
                raise NelixError(GENERATION_UNAVAILABLE,
                                 "epoch promotion failed for new incarnation")

        self._active = {
            "incarnation": inc, "epoch": epoch, "generation_id": gid,
            "transport": transport, "build_id": None, "build_id_probed": False,
        }
        build_id = None
        # Probe build_id for the new incarnation — informational, outside the lock.
        try:
            build_id = self._probe(transport)
            if build_id is not None:
                self._active["build_id"] = build_id
                self._active["build_id_probed"] = True
        except Exception:
            pass
        return GenerationHandle(generation_id=gid, epoch=epoch, transport=transport,
                                build_id=build_id, incarnation=inc)

    def topology_revision(self) -> int:
        """The vector cursor's topology component (nelix_contracts.cursor.new_cursor): bumped only
        when a generation is added to or removed from the registry (Plan 4). N=1 today, so this is
        a fixed 1 — never a daemon-incarnation restart, which changes `epoch`, not the topology."""
        with self._lock:
            return self._topology_revision

    def generations(self, *, discover=False) -> list:
        """The registry as a LIST (N=1 today): the active-generation pointer, or []. List-shaped so
        3c.2/Plan 4 can return N generations without reshaping this interface.

        `discover=True` (used ONLY by the fan-out board read, `router/board.py`): if nothing has
        been observed YET (`_active is None`), take one NON-SPAWNING discovery probe
        (`_discover_locked`, under the same lock `active()` uses) before answering `[]`.
        A router restart empties this registry but kills no daemon, so an unconditional `[]` would
        report an honestly-empty BOARD while a daemon already holds the singleton lock and serves
        live sessions the board must not hide. Only when that probe also finds nothing running is
        the board genuinely empty.

        The default (`discover=False` — /health, /capabilities, /generation_list, unchanged) never
        takes this probe: those routes answer strictly from what this registry has already observed,
        exactly as before this fix-pass; widening THEIR contract is not this fix."""
        with self._lock:
            if self._active is None and discover:
                self._discover_locked()
            return [] if self._active is None else [self._handle(self._active)]

    def _discover_locked(self):
        """Must be called with `self._lock` held, and only when `self._active is None`. A single
        NON-SPAWNING probe of the CURRENT singleton-lock holder (`supervisor.held_generation()`).
        If a daemon currently holds the lock, install it as the active generation — creating the
        generation row and a starting epoch. Unlike `active()`, this does NOT run a separate health
        check or CAS promote: the epoch is left `starting` and will be promoted by the next
        `active()` call that observes the same incarnation. `_ensure_available` is NOT called, so
        no daemon is spawned."""
        snap = self._sup.held_generation()
        if snap is None:
            return
        transport, inc = snap
        gid = self._generation_id
        clock = time.time()
        # create_generation if not already done (best-effort).
        if self._store is not None:
            try:
                self._store.create_generation(gid, build_id=None, lifecycle_state="active",
                                              capability_snapshot=None, created_at=clock)
            except Exception:
                return  # leave _active as None — board genuinely empty / not our job to fix
        epoch = new_generation_id()
        meta = _incarnation_meta(inc)
        if self._store is not None:
            try:
                self._store.insert_epoch(epoch, gid, incarnation_meta=meta, created_at=clock)
            except Exception:
                return
        self._active = {
            "incarnation": inc, "epoch": epoch, "generation_id": gid,
            "transport": transport, "build_id": None, "build_id_probed": False,
        }

    def _handle(self, a) -> GenerationHandle:
        return GenerationHandle(generation_id=a["generation_id"], epoch=a["epoch"],
                                transport=a["transport"], build_id=a["build_id"],
                                incarnation=a["incarnation"])

    def _ensure_available(self):
        """Make a HEALTHY generation available, OUTSIDE the lock (this is the slow work). Uses the
        full `active_generation()` read (which does the /health RPC); if nothing healthy is recorded,
        spawns one (ensure_running()) and re-checks. Returns nothing — the identity the caller pins
        is read separately UNDER the lock via `held_generation()` (the authoritative lock holder),
        so the epoch decision is atomic and never carries a stale outside-the-lock snapshot. Any
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
