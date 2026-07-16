"""The opaque vector cursor. Pure: no clock, no I/O.

`epoch:seq` is meaningful WITHIN one generation and nowhere else — two generations are two
processes with no common clock, so there is NO total order across them (design §4). The
cursor carries one position PER generation, plus the router's own epoch and the topology
revision it was minted against.

The token is OPAQUE to callers: they round-trip it, never parse it. It is base64url'd
compact JSON so that WE can debug it — not so that callers can read it.
"""
import base64
import json
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from .errors import BOARD_CHANGED, CURSOR_EXPIRED, INVALID_REQUEST, NelixError
from .ids import InvalidId, validate_generation_id

_V = 1
_MAX_TOKEN = 64 * 1024        # a cursor is small; anything larger is not one


def _freeze(positions):
    return MappingProxyType({str(g): (str(e), int(s)) for g, (e, s) in positions.items()})


@dataclass(frozen=True)
class Cursor:
    router_epoch: str
    topology_revision: int
    positions: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))

    def position_for(self, generation_id):
        """The (generation_epoch, seq) consumed for `generation_id`, or None if this cursor
        has never seen it — meaning: start from that generation's beginning."""
        return self.positions.get(generation_id)

    def advance(self, generation_id, generation_epoch, seq) -> "Cursor":
        """Advance ONLY this component. Never touch the others: an event delivered from
        generation A must not imply progress in B, or B's events are silently skipped.

        Within one generation_epoch the seq is monotonic — going backwards would re-deliver
        events the caller already handled. Across a generation_epoch change the seq
        legitimately restarts (that generation is a new process).
        """
        try:
            validate_generation_id(generation_id)
        except InvalidId as e:
            raise NelixError(INVALID_REQUEST, str(e)) from None
        # bool is an int subclass; a bool seq is a caller bug, not a 0/1 position.
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise NelixError(INVALID_REQUEST, f"seq must be a non-negative int: {seq!r}")
        epoch = str(generation_epoch)
        current = self.positions.get(generation_id)
        if current is not None and current[0] == epoch and seq < current[1]:
            raise NelixError(INVALID_REQUEST,
                             f"cursor may not rewind {generation_id}: {current[1]} -> {seq}")
        positions = dict(self.positions)
        positions[generation_id] = (epoch, seq)
        return replace(self, positions=_freeze(positions))


def new_cursor(router_epoch: str, topology_revision: int) -> Cursor:
    return Cursor(router_epoch=str(router_epoch),
                  topology_revision=int(topology_revision), positions=_freeze({}))


def encode(cursor: Cursor) -> str:
    raw = json.dumps(
        {"v": _V, "re": cursor.router_epoch, "tr": cursor.topology_revision,
         "p": {g: [e, s] for g, (e, s) in cursor.positions.items()}},
        separators=(",", ":"), sort_keys=True, allow_nan=False,
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(token, *, router_epoch: str, topology_revision: int) -> Cursor:
    """Decode and validate against the CURRENT router state.

    CURSOR_EXPIRED — the token's version is not ours, or the router was replaced (its epoch
    changed): either way the positions describe a world that no longer exists, so refetch.
    BOARD_CHANGED — the topology moved (a generation appeared or retired): the positions are
    still valid but the SET of components is not, so refetch and re-arm rather than silently
    missing a new generation's events.
    Neither is retryable verbatim; both mean "refetch the board".
    """
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN:
        raise NelixError(INVALID_REQUEST, "malformed cursor")
    try:
        pad = "=" * (-len(token) % 4)
        obj = json.loads(base64.urlsafe_b64decode(token + pad))
    except Exception:
        raise NelixError(INVALID_REQUEST, "malformed cursor") from None
    if not isinstance(obj, dict):
        raise NelixError(INVALID_REQUEST, "malformed cursor")
    # VERSION FIRST — before touching the body. A future version may legitimately have a
    # shape we cannot parse; reporting that as "malformed request" blames the caller for our
    # own upgrade. (rev 1 checked this last, so it only ever fired when the bump changed
    # nothing.)
    if obj.get("v") != _V:
        raise NelixError(CURSOR_EXPIRED, "cursor version is no longer supported")
    try:
        raw_positions = obj["p"]
        if not isinstance(raw_positions, dict):
            raise ValueError("positions must be an object")
        positions = {}
        for g, v in raw_positions.items():
            if not isinstance(v, list) or len(v) != 2:
                raise ValueError("position must be [epoch, seq]")
            seq = v[1]
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                raise ValueError("seq must be a non-negative int")
            validate_generation_id(g)
            positions[g] = (str(v[0]), seq)
        cursor = Cursor(router_epoch=str(obj["re"]),
                        topology_revision=int(obj["tr"]), positions=_freeze(positions))
    except Exception:
        raise NelixError(INVALID_REQUEST, "malformed cursor") from None
    if cursor.router_epoch != str(router_epoch):
        raise NelixError(CURSOR_EXPIRED, "router restarted; refetch the board")
    if cursor.topology_revision != int(topology_revision):
        raise NelixError(BOARD_CHANGED, "generation topology changed; refetch the board")
    return cursor
