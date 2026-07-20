"""nelix-3rm slice 3c.3a: the FAN-OUT board -- router GET /status with NO session_id.

The daemon's board-wide `/status` (`daemon/manager.py::status(session_id=None, *, owner_id)`) is
already OWNER-FILTERED and already carries a GLOBAL int cursor (per-GENERATION, meaningful only
within that generation -- `EventQueue.latest_seq()`). This module's job is entirely router-side:

  1. Forward that board to every generation the registry currently tracks (`registry.generations()`
     -- the same NON-SPAWNING snapshot /health, /capabilities and /generation_list read; a
     "read-only" board probe must not subprocess.Popen a daemon as a side effect either).
  2. MERGE the per-generation boards into ONE router-owned envelope via `merge_boards` -- a REAL
     N-way union (keyed by session_id, which is globally unique across generations, spec §3), not
     a hardcoded "return the one board": N=1 today, but the loop/union shape is exactly what Plan 4
     needs unchanged when a second generation appears.
  3. Attach an opaque VECTOR CURSOR (`nelix_contracts.cursor` -- reused, never hand-rolled): one
     component PER generation, keyed on that generation's STABLE `slot_id` (`router/registry.py`'s
     `GenerationHandle.slot_id` -- minted once, survives a daemon restart), with the VALUE
     `(that generation's epoch, that generation's own int cursor)`. Keying on `slot_id` rather than
     `epoch` (nelix-3rm 3c.3a fix-pass finding #1) is what lets a caller's cursor position for a
     generation SURVIVE that generation's daemon restarting: `epoch` is per-incarnation and would
     mint a fresh map key on every restart, orphaning the caller's prior position for it -- 3c.3b's
     `/wait` needs `position_for(slot_id)` to keep resolving to the SAME component across a restart,
     with only the epoch VALUE changing (so it can tell "same generation, new epoch" apart from "a
     generation that no longer exists").
  4. Never silently omit an UNAVAILABLE generation's sessions (spec §4): a forward failure (or an
     unhealthy-shaped reply) is recorded in `board_incomplete` by generation id, while every HEALTHY
     generation's results are still merged and returned -- always a 200, never a hard error, because
     the caller must see what IS available and know it is incomplete (BOARD_INCOMPLETE is
     retryable; nelix_contracts.errors). "Unhealthy-shaped" (fix-pass finding #2) is checked
     THOROUGHLY, not just "has a cursor key": a 200 reply whose `cursor` is not a non-negative int
     (a bool, a negative number, a string), or whose `sessions`/`recent_terminal` are missing or not
     objects, is treated exactly like a transport failure -- never merged as if healthy, and never
     left to crash `cursor.advance`/`merge_boards` into an uncaught error (which would turn a single
     misbehaving generation into a hard 400/500 for the whole board).
  5. A board read never lies "empty" just because THIS router process has observed nothing yet
     (fix-pass finding #3): an empty registry first takes one non-spawning discovery probe
     (`registry.generations()` -- see `router/registry.py`) for a daemon that is already running (a
     router restart kills no daemon), so a caller sees that daemon's sessions immediately rather than
     a false `board_incomplete: false` empty board that only self-heals once something else happens
     to touch the registry.

S2a.2 (this slice): the router also reads the archived board from the SHARED store DIRECTLY
and merges it with the per-generation LIVE boards. An archived terminal row is AUTHORITATIVE
for a session's terminal state and SUPPRESSES any live entry for the same session in the merged
result — the merged output never lists one session in both `sessions` and `recent_terminal`.
The cursor gains an archive component (archive_epoch, archive_seq) populated from the store read.

`/wait` (cursor DECODE + long-poll + CURSOR_EXPIRED/BOARD_CHANGED + advance) is 3c.3b, not this
slice -- this module only CONSTRUCTS and ENCODES the cursor.
"""
import sqlite3
import urllib.parse

from nelix_contracts.cursor import encode, new_cursor
from nelix_contracts.errors import (
    GENERATION_UNAVAILABLE, INVALID_REQUEST, STORE_UNAVAILABLE, NelixError,
)
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


def merge_archive_into(live, archive_records):
    """Merge archived terminal records into the live board.

    Archived terminal rows are AUTHORITATIVE: they SUPPRESS any live entry for the same
    session (in both `sessions` and `recent_terminal`). The merged output never lists one
    session in both maps.

    `live` is mutated in place (the merged dict from merge_boards). `archive_records` is
    the list of TerminalRecord from store.read_board_snapshot.
    """
    for tr in archive_records:
        sid = tr.session_id
        live["sessions"].pop(sid, None)
        live["recent_terminal"].pop(sid, None)
        entry = {
            "session_id": sid,
            "terminal_kind": tr.terminal_kind,
            "screen_excerpt": tr.summary,
            "control_state": "terminal",
            "pending": False,
            "terminal": True,
        }
        live["recent_terminal"][sid] = entry
    return live


class BoardForward:
    """Router GET /status with NO session_id -- the fan-out board read.

    S2a.2: also reads the archived board from the store and merges it into the live result.
    The store is optional (None when no store is available, such as in some test scenarios).
    """

    def __init__(self, registry, router_epoch, store=None, archive_epoch=None):
        if (store is None) != (archive_epoch is None):
            raise ValueError(
                "store and archive_epoch must be both set or both None; "
                f"got store={store!r}, archive_epoch={archive_epoch!r}")
        self._registry = registry
        self._router_epoch = router_epoch
        self._store = store
        self._archive_epoch = archive_epoch

    def status(self, owner_id) -> "tuple[int, dict]":
        owner_id = _owner(owner_id)
        # NON-SPAWNING (mirrors /health, /capabilities, /generation_list): a board read must never
        # spawn a generation as a side effect of what is otherwise a pure read. discover=True (fix-
        # pass finding #3): unlike those routes, the board must not report an honestly-empty result
        # while a daemon already holds the singleton lock -- see registry.generations()'s docstring.
        gens = self._registry.generations(discover=True)
        cursor = new_cursor(self._router_epoch, self._registry.topology_revision())
        healthy = []
        unavailable = []
        for gen in gens:
            reply = self._forward_one(gen, owner_id)
            if reply is None:
                unavailable.append(gen.generation_id)
                continue
            healthy.append((gen.generation_id, reply))
            # fix-pass finding #1: the cursor's map KEY is the STABLE slot_id (survives a daemon
            # restart); the per-incarnation epoch is carried as the VALUE, alongside the seq.
            cursor = cursor.advance(gen.generation_id, gen.epoch, reply["cursor"])
        merged = merge_boards(healthy)
        # S2a.2: read the archived board from the shared store and merge it.
        # An archived terminal is AUTHORITATIVE and suppresses any live entry for the same session.
        archive_incomplete = False
        if self._store is not None:
            try:
                archive_seq, records = self._store.read_board_snapshot(owner_id)
            except (sqlite3.Error, OSError):
                archive_incomplete = True
            except NelixError as e:
                if e.code == STORE_UNAVAILABLE:
                    archive_incomplete = True
                else:
                    raise
            else:
                merged = merge_archive_into(merged, records)
                cursor = cursor.advance_archive(self._archive_epoch, archive_seq)
                # C1: advance confirmed_high_water per epoch using the per-terminal
                # terminal_seq (not the cross-owner archive_seq). For each terminal
                # record resolved as board-visible, advance its epoch's confirmed
                # watermark to at least that terminal's seq. Also resolve acked/expired
                # terminals (absent from records but already visible/validly expired).
                if self._store is not None and records:
                    for tr in records:
                        epoch = getattr(tr, "generation_epoch", None)
                        tseq = getattr(tr, "terminal_seq", None)
                        if epoch and tseq is not None:
                            try:
                                self._store.set_generation_confirmed_high_water(
                                    epoch, tseq)
                            except (sqlite3.Error, NelixError):
                                pass
        merged["cursor"] = encode(cursor)
        merged["board_incomplete"] = unavailable if unavailable else False
        if archive_incomplete:
            merged["archive_incomplete"] = True
        return 200, merged

    def _forward_one(self, gen, owner_id):
        """Forward the board-wide /status to ONE generation. Returns its decoded board dict, or
        None if it is UNAVAILABLE (a transport failure of either phase, via the shared `relay`
        mapping -- reused, never re-derived -- or a reply that does not even look like the daemon's
        own board shape). None is the caller's signal to record `gen.generation_id` in `board_incomplete`
        rather than silently treating a down generation as having no sessions.

        fix-pass finding #2: a 200 reply is not trusted just because it is a dict with a "cursor"
        key -- that let a malformed-but-200 reply (`{"cursor": 12}`, no sessions/recent_terminal) get
        silently MERGED as an empty-but-healthy generation (the exact silent omission spec §4
        forbids), and let `{"cursor": -1}` / `{"cursor": "abc"}` / `{"sessions": "oops"}` pass this
        gate only to crash `cursor.advance`/`merge_boards` downstream into an uncaught NelixError/
        TypeError -- a hard 400/500 for the WHOLE board over one misbehaving generation, violating
        "never a hard error, always board_incomplete". So every field this method or its callers
        will actually read is shape-checked HERE, before anything is trusted as healthy."""
        client = RpcClient(gen.transport, owner_id)
        path = "/status?" + urllib.parse.urlencode({"owner_id": owner_id})
        try:
            status, body = relay(lambda: client.forward_raw("GET", path, None))
        except NelixError as e:
            if e.code != GENERATION_UNAVAILABLE:
                raise
            return None
        if status != 200 or not isinstance(body, dict) or not self._is_healthy_board(body):
            # The generation answered but not with a healthy board -- never fabricate an empty
            # board indistinguishable from "no sessions" (spec §4); treat it the same as
            # unreachable.
            return None
        return body

    @staticmethod
    def _is_healthy_board(body) -> bool:
        """True iff `body` has every shape `merge_boards`/`cursor.advance` will actually rely on:
        a `cursor` that is a non-negative int (`bool` is an `int` subclass in Python -- rejected
        explicitly, a cursor is never a truth value), and `sessions`/`recent_terminal` are PRESENT
        and are objects (dicts) -- the real daemon's own board-wide status() (daemon/manager.py)
        always sends both (empty dicts, never omitted), so requiring them closes the exact
        `{"cursor": 12}` silent-omission case fix-pass finding #2 named, not merely reachable ones."""
        cursor = body.get("cursor")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            return False
        for key in ("sessions", "recent_terminal"):
            if not isinstance(body.get(key), dict):
                return False
        return True
