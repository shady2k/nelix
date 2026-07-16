"""The opaque vector cursor. Pure: no clock, no I/O.

`epoch:seq` is meaningful WITHIN one generation and nowhere else — two generations are two
processes with no common clock, so there is NO total order across them (design §4). The
cursor therefore carries one position PER generation, plus the router's own epoch and the
topology revision it was minted against.

The token is OPAQUE to callers: they round-trip it, never parse it. It is base64url'd
compact JSON so that WE can debug it — not so that callers can read it.
"""
import base64
import json
from dataclasses import dataclass, field, replace

from .errors import BOARD_CHANGED, CURSOR_EXPIRED, INVALID_REQUEST, NelixError

_V = 1


@dataclass(frozen=True)
class Cursor:
    router_epoch: str
    topology_revision: int
    positions: dict = field(default_factory=dict)   # generation_id -> (generation_epoch, seq)

    def position_for(self, generation_id):
        """The (generation_epoch, seq) consumed for `generation_id`, or None if this cursor
        has never seen it — meaning: start from that generation's beginning."""
        return self.positions.get(generation_id)

    def advance(self, generation_id, generation_epoch, seq) -> "Cursor":
        """Advance ONLY this component. Never touch the others: an event delivered from
        generation A must not imply progress in B, or B's events are silently skipped."""
        positions = dict(self.positions)
        positions[generation_id] = (str(generation_epoch), int(seq))
        return replace(self, positions=positions)

    def __eq__(self, other):
        return (isinstance(other, Cursor)
                and self.router_epoch == other.router_epoch
                and self.topology_revision == other.topology_revision
                and self.positions == other.positions)


def new_cursor(router_epoch: str, topology_revision: int) -> Cursor:
    return Cursor(router_epoch=str(router_epoch),
                  topology_revision=int(topology_revision), positions={})


def encode(cursor: Cursor) -> str:
    raw = json.dumps(
        {"v": _V, "re": cursor.router_epoch, "tr": cursor.topology_revision,
         "p": {g: [e, s] for g, (e, s) in cursor.positions.items()}},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(token, *, router_epoch: str, topology_revision: int) -> Cursor:
    """Decode and validate against the CURRENT router state.

    CURSOR_EXPIRED — the router was replaced (its epoch changed): the positions describe a
    routing world that no longer exists, so the caller must refetch the board.
    BOARD_CHANGED — the topology moved (a generation appeared or retired): the positions are
    still valid but the SET of components is not, so the caller refetches and re-arms rather
    than silently missing a new generation's events.
    Neither is retryable verbatim; both mean "refetch the board".
    """
    if not isinstance(token, str) or not token:
        raise NelixError(INVALID_REQUEST, "malformed cursor")
    try:
        pad = "=" * (-len(token) % 4)
        obj = json.loads(base64.urlsafe_b64decode(token + pad))
        version = obj["v"]
        raw_positions = obj["p"]
        cursor = Cursor(
            router_epoch=obj["re"], topology_revision=int(obj["tr"]),
            positions={g: (str(v[0]), int(v[1])) for g, v in raw_positions.items()},
        )
    except NelixError:
        raise
    except Exception:
        raise NelixError(INVALID_REQUEST, "malformed cursor") from None
    if version != _V:
        raise NelixError(CURSOR_EXPIRED, "cursor version is no longer supported")
    if cursor.router_epoch != str(router_epoch):
        raise NelixError(CURSOR_EXPIRED, "router restarted; refetch the board")
    if cursor.topology_revision != int(topology_revision):
        raise NelixError(BOARD_CHANGED, "generation topology changed; refetch the board")
    return cursor
