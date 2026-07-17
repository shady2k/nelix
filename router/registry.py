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
    incarnation is keyed on supervisor identity — (pid, start_fingerprint) via supervisor.incarnation().
  * `transport` — the live daemon Transport (from supervisor.endpoint(), else ensure_running()).
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
    """Thread-safe: the router shares ONE registry across its request threads. `active()` runs under
    a single lock so two concurrent starts observing a fresh incarnation mint exactly ONE epoch (not
    two racing ids for the same generation)."""

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
        NelixError(GENERATION_UNAVAILABLE) if none can be made available."""
        with self._lock:
            transport = self._ensure_transport()
            inc = self._sup.incarnation()
            if inc is None:
                # The transport answered but no live, fingerprint-matched incarnation is recorded (a
                # tight race between the endpoint probe and the state read). Retryable.
                raise NelixError(GENERATION_UNAVAILABLE,
                                 "no live generation incarnation is recorded")
            if self._active is None or self._active["incarnation"] != inc:
                # First observation of THIS incarnation -> mint a fresh epoch and read its build-id.
                self._active = {"incarnation": inc, "epoch": new_generation_id(),
                                "build_id": self._probe(transport), "transport": transport}
            else:
                self._active["transport"] = transport      # same incarnation: refresh the transport
            return self._handle(self._active)

    def generations(self) -> list:
        """The registry as a LIST (N=1 today): the active-generation pointer, or []. List-shaped so
        3c.2/Plan 4 can return N generations without reshaping this interface."""
        with self._lock:
            return [] if self._active is None else [self._handle(self._active)]

    @staticmethod
    def _handle(a) -> GenerationHandle:
        return GenerationHandle(epoch=a["epoch"], transport=a["transport"],
                                build_id=a["build_id"], incarnation=a["incarnation"])

    def _ensure_transport(self):
        """The live backend Transport: the running daemon (supervisor.endpoint()), else spawn one
        (ensure_running()). Any failure to produce one is GENERATION_UNAVAILABLE (retryable)."""
        try:
            transport = self._sup.endpoint()
            if transport is None:
                transport = self._sup.ensure_running()
            if transport is None:
                raise NelixError(GENERATION_UNAVAILABLE, "no generation backend available")
            return transport
        except NelixError:
            raise
        except Exception as e:
            raise NelixError(GENERATION_UNAVAILABLE,
                             f"could not make a generation available: {e}") from None
