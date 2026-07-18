"""nelix-3rm slice 3c.2 Part C: router-LOCAL OPERATOR routes — capabilities and generation_list.

routing.classify keeps both OPERATOR (never SESSION_KEYED/FAN_OUT) on purpose: capabilities is
router-local precisely so a per-generation answer is never merged across N generations (fanning it
out would defeat a per-session capability check — see nelix_contracts.routing's docstring), and the
topology read (generation_list) is inherently about the router's OWN registry, not any one caller's
session. Neither owner-gates: they answer from the registry / the one active generation's global
facts, not from a caller's session.
"""
import urllib.parse

from nelix_contracts.errors import GENERATION_UNAVAILABLE, NelixError

from router.forwarding import relay
from router.registry import PROBE_OWNER

try:
    from rpc_client import RpcClient
except ImportError:                                          # package mode
    from .rpc_client import RpcClient


class OperatorRoutes:
    def __init__(self, registry, router_epoch):
        self._registry = registry
        self._router_epoch = router_epoch

    def generation_list(self):
        """The registry's topology (size 1 today): each tracked generation's router-minted
        generation_id (the exact value /start's response calls `generation_id` — the StartLedger's
        key, spec §3), the daemon's own informational build_id (may be null in dev), and its
        transport kind. Reads registry.generations() — the same NON-SPAWNING snapshot /health
        reads — never registry.active(): listing the topology must not itself spawn a generation."""
        gens = self._registry.generations()
        return 200, {
            "router_epoch": self._router_epoch,
            "generations": [
                {"generation_id": g.epoch, "build_id": g.build_id,
                 "transport_kind": getattr(g.transport, "kind", None)}
                for g in gens
            ],
        }

    def capabilities(self):
        """Minimal + honest (brief): the router's own identity + the ONE active generation's real
        global /capabilities baseline, forwarded verbatim (never fabricated). Per-session
        capabilities are not built here — classify() only requires this route stay router-local and
        unfanned, not that it resolve a specific session; the global baseline already satisfies that
        honestly for a single-generation router. PROBE_OWNER (registry.py) constructs the RpcClient
        exactly as the health probe does: /capabilities requires an owner_id on the wire, but this
        call carries no real caller to source one from.

        Reads registry.generations() — the same NON-SPAWNING snapshot /health and generation_list
        read — never registry.active(): a "read-only" capabilities probe must not spawn a daemon as
        a side effect (that would contradict /health's and /generation_list's own honesty). If no
        generation is currently recorded, this is GENERATION_UNAVAILABLE (retryable), exactly what
        /health would imply by reporting no active_generation — never a spawn-and-wait."""
        gens = self._registry.generations()
        if not gens:
            raise NelixError(GENERATION_UNAVAILABLE, "no generation is currently available")
        gen = gens[0]
        client = RpcClient(gen.transport, PROBE_OWNER)
        path = "/capabilities?" + urllib.parse.urlencode({"owner_id": PROBE_OWNER})
        status, body = relay(lambda: client.forward_raw("GET", path, None))
        return status, {"router_epoch": self._router_epoch, "generation_id": gen.epoch,
                        "capabilities": body}
