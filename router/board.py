"""nelix-3rm slice 3c.3a: the FAN-OUT board -- router GET /status with NO session_id.

The daemon's board-wide `/status` (`daemon/manager.py::status(session_id=None, *, owner_id)`) is
already OWNER-FILTERED and already carries a GLOBAL int cursor (per-GENERATION, meaningful only
within that generation -- `EventQueue.latest_seq()`). This module's job is entirely router-side:

  1. Forward that board to every generation the registry currently tracks (`registry.generations()`
     -- the same NON-SPAWNING snapshot /health, /capabilities and /generation_list already read; a
     "read-only" board probe must not subprocess.Popen a daemon as a side effect either).
  2. MERGE the per-generation boards into ONE router-owned envelope via `merge_boards` -- a REAL
     N-way union (keyed by session_id, which is globally unique across generations, spec §3), not
     a hardcoded "return the one board": N=1 today, but the loop/union shape is exactly what Plan 4
     needs unchanged when a second generation appears.
  3. Attach an opaque VECTOR CURSOR (`nelix_contracts.cursor` -- reused, never hand-rolled): one
     component PER generation, `(that generation's epoch, that generation's own int cursor)`. The
     epoch doubles as the cursor's `generation_id` key because that IS this registry's generation
     identity today (the same value `/start`, `/generation_list` and the StartLedger already call
     `generation_id` -- see `router/registry.py`); Plan 4, when a generation's identity outlives its
     epoch (a slot that survives a daemon restart), would key on that stable id instead.
  4. Never silently omit an UNAVAILABLE generation's sessions (spec §4): a forward failure (or an
     unhealthy-shaped reply) is recorded in `board_incomplete` by generation id, while every HEALTHY
     generation's results are still merged and returned -- always a 200, never a hard error, because
     the caller must see what IS available and know it is incomplete (BOARD_INCOMPLETE is
     retryable; nelix_contracts.errors).

`/wait` (cursor DECODE + long-poll + CURSOR_EXPIRED/BOARD_CHANGED + advance) is 3c.3b, not this
slice -- this module only CONSTRUCTS and ENCODES the cursor.
"""
import urllib.parse

from nelix_contracts.cursor import encode, new_cursor
from nelix_contracts.errors import GENERATION_UNAVAILABLE, INVALID_REQUEST, NelixError
from nelix_contracts.ids import InvalidId, validate_owner_id

from router.forwarding import relay

try:
    from rpc_client import RpcClient
except ImportError:                                          # package mode
    from .rpc_client import RpcClient


def _owner(value):
    try:
        return validate_owner_id(value)
    except InvalidId as e:
        raise NelixError(INVALID_REQUEST, str(e)) from None


def merge_boards(per_generation) -> dict:
    """The N-WAY merge (Plan-4-ready): `per_generation` is an iterable of
    `(generation_id, board_dict)` for HEALTHY generations only -- an unavailable generation is
    never passed here (BoardForward.status tracks it in `board_incomplete` instead, never as a
    silently-empty entry in this union).

    Each `board_dict` is the daemon's OWN board-wide `/status` shape, already owner-FILTERED
    server-side (`sessions`, `recent_terminal`, plus fields this function ignores -- `cursor`,
    `limit`, `rpc_protocol` -- which are per-generation facts BoardForward reads separately, not
    merged data). `sessions`/`recent_terminal` are keyed by session_id, which is GLOBALLY unique
    across generations (spec §3), so a UNION is safe: no two healthy generations should ever report
    the same key. N=1 collapses this to "return the one board's sessions/recent_terminal" -- but
    doing it via `dict.update` in a loop (not `return per_generation[0][1]`) is what lets Plan 4
    add a second generation without reshaping this function.
    """
    sessions = {}
    recent_terminal = {}
    for _generation_id, board in per_generation:
        sessions.update(board.get("sessions") or {})
        recent_terminal.update(board.get("recent_terminal") or {})
    return {"sessions": sessions, "recent_terminal": recent_terminal}


class BoardForward:
    """Router GET /status with NO session_id -- the fan-out board read."""

    def __init__(self, registry, router_epoch):
        self._registry = registry
        self._router_epoch = router_epoch

    def status(self, owner_id) -> "tuple[int, dict]":
        owner_id = _owner(owner_id)
        # NON-SPAWNING (mirrors /health, /capabilities, /generation_list): a board read must never
        # spawn a generation as a side effect of what is otherwise a pure read.
        gens = self._registry.generations()
        cursor = new_cursor(self._router_epoch, self._registry.topology_revision())
        healthy = []
        unavailable = []
        for gen in gens:
            reply = self._forward_one(gen, owner_id)
            if reply is None:
                unavailable.append(gen.epoch)
                continue
            healthy.append((gen.epoch, reply))
            # generation_id == generation_epoch today (see module docstring): the registry's ONLY
            # identity for a generation IS its per-incarnation epoch.
            cursor = cursor.advance(gen.epoch, gen.epoch, reply["cursor"])
        merged = merge_boards(healthy)
        merged["cursor"] = encode(cursor)
        merged["board_incomplete"] = unavailable if unavailable else False
        return 200, merged

    def _forward_one(self, gen, owner_id):
        """Forward the board-wide /status to ONE generation. Returns its decoded board dict, or
        None if it is UNAVAILABLE (a transport failure of either phase, via the shared `relay`
        mapping -- reused, never re-derived -- or a reply that does not even look like the daemon's
        own board shape). None is the caller's signal to record `gen.epoch` in `board_incomplete`
        rather than silently treating a down generation as having no sessions."""
        client = RpcClient(gen.transport, owner_id)
        path = "/status?" + urllib.parse.urlencode({"owner_id": owner_id})
        try:
            status, body = relay(lambda: client.forward_raw("GET", path, None))
        except NelixError as e:
            if e.code != GENERATION_UNAVAILABLE:
                raise
            return None
        if status != 200 or not isinstance(body, dict) or "cursor" not in body:
            # The generation answered but not with a healthy board -- never fabricate an empty
            # board indistinguishable from "no sessions" (spec §4); treat it the same as
            # unreachable.
            return None
        return body
