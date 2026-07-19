"""The opaque vector cursor. Pure: no clock, no I/O.

`epoch:seq` is meaningful WITHIN one generation and nowhere else — two generations are two
processes with no common clock, so there is NO total order across them (design §4). The
cursor carries one position PER generation, plus the router's own epoch and the topology
revision it was minted against.

The token is OPAQUE to callers: they round-trip it, never parse it. It is base64url'd
compact JSON so that WE can debug it — not so that callers can read it.

S2a.1: added a DISTINCT typed archive component (archive_epoch, archive_seq) alongside the
generation positions, and bumped _V to 2 so old tokens yield CURSOR_EXPIRED.
"""
import base64
import json
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Optional

from .errors import BOARD_CHANGED, CURSOR_EXPIRED, INVALID_REQUEST, NelixError
from .ids import InvalidId, validate_generation_id

_V = 2
_MAX_TOKEN = 64 * 1024        # a cursor is small; anything larger is not one


def _freeze(positions):
    return MappingProxyType({str(g): (str(e), int(s)) for g, (e, s) in positions.items()})


@dataclass(frozen=True)
class Cursor:
    router_epoch: str
    topology_revision: int
    positions: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    _archive: Optional[tuple] = field(default=None, repr=False)   # (archive_epoch, archive_seq) | None

    @property
    def archive_position(self):
        """(archive_epoch, archive_seq) or None if no archive component has been set."""
        return self._archive

    def __post_init__(self):
        if not isinstance(self.router_epoch, str) or not self.router_epoch:
            raise NelixError(INVALID_REQUEST,
                             f"router_epoch must be a non-empty string: {self.router_epoch!r}")
        if (isinstance(self.topology_revision, bool)
                or not isinstance(self.topology_revision, int)
                or self.topology_revision < 0):
            raise NelixError(INVALID_REQUEST,
                             f"topology_revision must be a non-negative int: "
                             f"{self.topology_revision!r}")
        for generation_id, position in dict(self.positions).items():
            try:
                validate_generation_id(generation_id)
            except InvalidId as e:
                raise NelixError(INVALID_REQUEST, str(e)) from None
            if (not isinstance(position, tuple) or len(position) != 2
                    or not isinstance(position[0], str)
                    or isinstance(position[1], bool)
                    or not isinstance(position[1], int) or position[1] < 0):
                raise NelixError(INVALID_REQUEST,
                                 f"position for {generation_id} must be (str, non-negative "
                                 f"int): {position!r}")
        # Freeze whatever we were handed, so a caller who passed a plain dict cannot mutate
        # the cursor afterwards. object.__setattr__ because the dataclass is frozen.
        object.__setattr__(self, "positions", MappingProxyType(dict(self.positions)))
        # Validate archive component if present.
        arch = self._archive
        if arch is not None:
            if (not isinstance(arch, tuple) or len(arch) != 2
                    or isinstance(arch[1], bool)
                    or not isinstance(arch[1], int) or arch[1] < 0
                    or not isinstance(arch[0], int)):
                raise NelixError(INVALID_REQUEST,
                                 f"archive must be (int, non-negative int) or None: {arch!r}")
            object.__setattr__(self, "_archive", (int(arch[0]), int(arch[1])))

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

    def advance_archive(self, archive_epoch: int, seq: int) -> "Cursor":
        """Advance ONLY the archive component. Leaves generation positions untouched.

        Returns a new Cursor with the archive updated to (archive_epoch, seq).
        Both values are validated exactly as generation positions are.
        """
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise NelixError(INVALID_REQUEST,
                             f"archive seq must be a non-negative int: {seq!r}")
        if isinstance(archive_epoch, bool) or not isinstance(archive_epoch, int):
            raise NelixError(INVALID_REQUEST,
                             f"archive_epoch must be an int: {archive_epoch!r}")
        return replace(self, _archive=(int(archive_epoch), int(seq)))


def new_cursor(router_epoch: str, topology_revision: int) -> Cursor:
    return Cursor(router_epoch=str(router_epoch),
                  topology_revision=int(topology_revision), positions=_freeze({}))


def encode(cursor: Cursor) -> str:
    obj = {"v": _V, "re": cursor.router_epoch, "tr": cursor.topology_revision,
           "p": {g: [e, s] for g, (e, s) in cursor.positions.items()}}
    if cursor.archive_position is not None:
        obj["a"] = list(cursor.archive_position)
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()
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
    if obj.get("v") is not _V:          # `is not`: True == 1, and JSON true is not our version
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
        # NOTE: named distinctly from the `topology_revision` PARAMETER above (the caller's
        # current value) — reusing that name here shadowed it, so the router-mismatch check
        # below silently compared the decoded value against itself and never fired.
        decoded_topology_revision = obj["tr"]
        if (isinstance(decoded_topology_revision, bool)
                or not isinstance(decoded_topology_revision, int)):
            raise ValueError("topology_revision must be an int")
        # Decode archive component (optional — absent means None).
        raw_archive = obj.get("a")
        archive = None
        if raw_archive is not None:
            if not isinstance(raw_archive, list) or len(raw_archive) != 2:
                raise ValueError("archive must be [epoch, seq]")
            a_seq = raw_archive[1]
            if isinstance(a_seq, bool) or not isinstance(a_seq, int) or a_seq < 0:
                raise ValueError("archive seq must be a non-negative int")
            if isinstance(raw_archive[0], bool) or not isinstance(raw_archive[0], int):
                raise ValueError("archive epoch must be an int")
            archive = (int(raw_archive[0]), int(a_seq))
        cursor = Cursor(router_epoch=str(obj["re"]),
                        topology_revision=decoded_topology_revision,
                        positions=_freeze(positions),
                        _archive=archive)
    except Exception:
        raise NelixError(INVALID_REQUEST, "malformed cursor") from None
    if cursor.router_epoch != str(router_epoch):
        raise NelixError(CURSOR_EXPIRED, "router restarted; refetch the board")
    if cursor.topology_revision != int(topology_revision):
        raise NelixError(BOARD_CHANGED, "generation topology changed; refetch the board")
    return cursor
