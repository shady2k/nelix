"""The router's generation registry — ONE generation today, structurally multi-generation.

The registry owns the "active-generation pointer" (spec §1): new sessions route to the one
generation it tracks. It is deliberately LIST-shaped (a registry of N, N=1 today) so 3c.2/Plan 4
add generations without reshaping this — nothing hard-codes "one generation" in a way a later slice
must tear out.

Per generation it tracks:
  * `epoch` — a per-INCARNATION `generation_epoch` MINTED by the router (`new_generation_id()`, a
    g-<32hex> id — the exact shape the StartLedger stores and validates). The epoch is what
    assign_generation/commit bind a reservation to; keyed on the daemon's INCARNATION so a daemon
    RESTART yields a NEW epoch (spec §4: "a fresh generation epoch is minted on EVERY incarnation").
    The daemon's own /health carries no incarnation id yet (that addition is a later slice), so the
    incarnation is keyed on supervisor identity — (pid, start_fingerprint).
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
    """A snapshot of one tracked generation. `epoch` is the router's per-incarnation id (the ledger
    key); `transport` reaches the backend; `build_id` is the daemon's self-reported identity (or
    None); `incarnation` is the supervisor identity the epoch is keyed on."""
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
        # The active-generation pointer, N=1: {"incarnation", "epoch", "build_id", "transport"} or
        # None before anything is observed. A dict, not the frozen handle, so the transport can be
        # refreshed in place while the epoch/build_id stay pinned to the incarnation.
        self._active = None

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
        return GenerationHandle(epoch=epoch, transport=transport, build_id=build_id, incarnation=inc)

    def generations(self) -> list:
        """The registry as a LIST (N=1 today): the active-generation pointer, or []. List-shaped so
        3c.2/Plan 4 can return N generations without reshaping this interface."""
        with self._lock:
            return [] if self._active is None else [self._handle(self._active)]

    @staticmethod
    def _handle(a) -> GenerationHandle:
        return GenerationHandle(epoch=a["epoch"], transport=a["transport"],
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
