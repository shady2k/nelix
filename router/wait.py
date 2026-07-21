"""nelix-3rm slice 3c.3b: the router ORCHESTRATION-scoped /wait -- one waiter for the N workers of
an orchestration, long-polling the generation(s) via the opaque vector cursor (spec §1 fan-out, §4
cursors, §10 "orchestration is the safe middle"). Builds on the merged board + vector cursor (3c.3a,
router/board.py); reuses nelix_contracts.cursor, the registry, and the phase-split forward -- never
hand-rolls a second cursor or a second error mapping.

The shape, and why each piece is where it is:

   1. DERIVE THE SESSIONS from the OWNER-SCOPED StartLedger (`sessions_for_orchestration`). This is
      the router's isolation seam: owner Y querying owner Z's orchestration_id gets NONE of Z's
      sessions (the ledger filters on owner_id too), so a foreign orchestration collapses to an
      EMPTY set here, before any generation is touched. An empty set is a wait that can NEVER wake ->
      an EXPLICIT `empty_orchestration` marker, never a silent 25s null spin the caller re-issues
      forever (the same anti-spin reasoning the daemon's un-armable single /wait 404 embodies).

   2. DECODE THE VECTOR CURSOR against the CURRENT router state (nelix_contracts.cursor.decode). A
      router restart changed router_epoch -> CURSOR_EXPIRED; the topology moved (a generation
      appeared/retired) -> BOARD_CHANGED. Both mean "refetch the board and re-arm", so both are
      surfaced as EXPLICIT 200 markers ({cursor_expired:true} / {board_changed:true}) -- the SAME
      resync-marker shape the daemon's own /wait uses for a fallen-off ring cursor, so a caller has
      ONE uniform place to detect "resync" rather than a mix of body flags and error envelopes. A
      MISSING cursor = "start from now": read the generation's current int cursor and arm from there,
      so only a NEW event wakes (never re-delivering old history).

   3. FOR THE GENERATION(S) (N=1 today, LIST-shaped for Plan 4): take the cursor's component for the
      generation's STABLE `slot_id` (`Cursor.position_for`). If the component's EPOCH != the
      generation's CURRENT epoch, the daemon restarted (a fresh incarnation minted a fresh epoch, so
      seqs reset) -> CURSOR_EXPIRED, never a wait on a stale epoch's seq. Else forward the daemon's
      MULTI-SESSION wait (`/wait` with repeated `session_id=`) for (the orchestration's sessions on
      this generation, after_seq = the component's seq), OWNER PASSED THROUGH -- the daemon (not the
      router) owner-gates each sid, exactly as the session-keyed forwards relay ownership to the one
      real gate. On an event -> ADVANCE ONLY that component (spec §4: "return when ANY backend
      produces an event -> advance ONLY that component") and return {event, cursor}. On timeout ->
      the UNCHANGED cursor. On the generation's cursor_expired -> the marker. On an all-unowned set
      the daemon 404s -> an explicit `unownable` signal.

   N=1 collapses the generation loop to a single forward; the structure (a per-generation subset +
   after_seq, racing, returning on the FIRST event) is exactly what Plan 4 needs when a session's
   ledger `generation_id` resolves it to one of several slots. `registry.active()` -- the SAME
   authoritative current-generation read the session-keyed forwards use -- gives the current
   (transport, epoch); a wait is a session/orchestration operation, not the pure fan-out READ the
   board is, so it resolves the live generation like respond/stop/restart, not the non-spawning
   board-discovery probe.

S2a.3 (this slice): the archive wake arm (bounded cross-process poll on board_seq). The
WaitForward now also polls the store for archived mutations (ack, expiry, prune) which produce
NO daemon ring event. The generation /wait is multiplexed with a SHORT bounded timeout so the
router can poll board_seq and wake on archived mutations within the same ~25s window — no
orphaned 25s requests. Archive read failures degrade gracefully (narrow catch), never a false
GENERATION_UNAVAILABLE.
"""
import sqlite3
import time
import urllib.parse

from nelix_contracts.cursor import decode, encode, new_cursor
from nelix_contracts.errors import (
    BOARD_CHANGED, CURSOR_EXPIRED, GENERATION_UNAVAILABLE, INVALID_REQUEST, STORE_UNAVAILABLE,
    NelixError,
)
from nelix_contracts.ids import InvalidId, validate_orchestration_id, validate_owner_id

from router.forwarding import relay
from router.waiters import WaiterRegistry

try:
    from rpc_client import RpcClient
except ImportError:                                          # package mode
    from .rpc_client import RpcClient


def _owner(value):
    try:
        return validate_owner_id(value)
    except InvalidId as e:
        raise NelixError(INVALID_REQUEST, str(e)) from None


def _orchestration(value):
    try:
        return validate_orchestration_id(value)
    except InvalidId as e:
        raise NelixError(INVALID_REQUEST, str(e)) from None


def _healthy_int_cursor(value) -> bool:
    # bool is an int subclass; a cursor is never a truth value (mirrors board._is_healthy_board).
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class WaitForward:
    """Router GET /wait -- the orchestration-scoped long-poll."""

    # S2a.3: the archive multiplex window and bounded poll interval (seconds). Made
    # class-level so tests can override for fast execution without real blocking.
    _WAIT_WINDOW = 25.0
    _POLL_INTERVAL = 1.0

    def __init__(self, ledger, registry, router_epoch, store=None, archive_epoch=None,
                 waiters=None):
        if (store is None) != (archive_epoch is None):
            raise ValueError(
                "store and archive_epoch must be both set or both None; "
                f"got store={store!r}, archive_epoch={archive_epoch!r}")
        self._ledger = ledger
        self._registry = registry
        self._router_epoch = router_epoch
        self._store = store
        self._archive_epoch = archive_epoch
        self._waiters = waiters if waiters is not None else WaiterRegistry()

    def wait(self, owner_id, orchestration_id, cursor_token) -> "tuple[int, dict]":
        """Long-poll the orchestration. The effective long-poll window is always the generation's
        fixed ~25s (the single-session /wait is likewise not per-call tunable) -- there is no
        per-call timeout knob, so callers must not be offered one that would silently do nothing."""
        owner_id = _owner(owner_id)
        orchestration_id = _orchestration(orchestration_id)

        # The waiter count covers EVERY path that can block and NO path that merely validates: the
        # two id validations above can reject before anyone is attached, and everything after this
        # line is inside the count. That is what makes "waiters > 0" mean "someone is listening",
        # never "someone sent a malformed id".
        with self._waiters.attached(orchestration_id):
            return self._wait_inner(owner_id, orchestration_id, cursor_token)

    def _wait_inner(self, owner_id, orchestration_id, cursor_token) -> "tuple[int, dict]":
        # 1. The orchestration's waitable sessions (owner-scoped). Empty -> can never wake.
        sessions = self._ledger.sessions_for_orchestration(owner_id, orchestration_id)
        if not sessions:
            return 200, {"event": None, "empty_orchestration": True}

        # 2. Decode (or freshly build) the vector cursor. decode's CURSOR_EXPIRED / BOARD_CHANGED
        #    are resync conditions -> explicit 200 markers (uniform with the daemon's own /wait);
        #    a truly malformed cursor stays a hard INVALID_REQUEST (a caller bug, not a resync).
        topology = self._registry.topology_revision()
        if cursor_token:
            try:
                cursor = decode(cursor_token, router_epoch=self._router_epoch,
                                topology_revision=topology)
            except NelixError as e:
                if e.code == CURSOR_EXPIRED:
                    return 200, {"event": None, "cursor_expired": True}
                if e.code == BOARD_CHANGED:
                    return 200, {"event": None, "board_changed": True}
                raise
        else:
            cursor = new_cursor(self._router_epoch, topology)

        # 3. The generation(s). N=1 today; active() = the authoritative current generation (the same
        #    read the session-keyed forwards use), raising GENERATION_UNAVAILABLE (retryable) if none
        #    can be made available.
        gen = self._registry.active()
        # Plan 4: partition `sessions` by their ledger generation_id -> slot, derive (subset,
        # after_seq) PER generation, forward each wait, and return on the FIRST event -> advancing
        # ONLY that generation's component. N=1 collapses to one generation owning every session:
        gen_sessions = sessions

        component = cursor.position_for(gen.generation_id)
        if component is None:
            # No prior position for this generation (a fresh/board-less cursor) -> start from NOW:
            # its current int cursor, so only a NEW event wakes. Establish the component so the
            # returned token is coherent (and a timeout re-arm does not re-read "now" every call).
            after_seq = self._current_seq(gen, owner_id)
            cursor = cursor.advance(gen.generation_id, gen.epoch, after_seq)
        else:
            epoch, after_seq = component
            if epoch != gen.epoch:
                # The daemon restarted: a fresh incarnation minted a fresh epoch, so this seq is from
                # a seq-space that no longer exists. Resync -- never wait on a stale epoch's seq.
                return 200, {"event": None, "cursor_expired": True}

        # TRANSIENT (self-healing; NOT fixed here -- this is a DAEMON restart mid-wait, a different
        # event from the ROUTER restart nelix-3rm 3c.4 covers, and 3c.4 explicitly leaves it deferred):
        # this epoch check and the forward below are not atomic. If the daemon restarts in the GAP
        # between this check passing and the daemon actually receiving the forwarded /wait, the
        # request lands on the NEW incarnation (seq counters reset) still carrying the OLD after_seq.
        # That one wait wastes its ~25s window (times out, no event) -- but it SELF-HEALS: the
        # caller's next /wait re-captures the (now current) generation handle, this same epoch check
        # sees the new incarnation's epoch != the cursor's recorded epoch, and returns cursor_expired
        # IMMEDIATELY, so the caller resyncs via /status. No data loss -- the board is the source of
        # truth. The robust fix -- the daemon's /wait reply carrying its own incarnation/epoch so the
        # router can detect the mismatch on THIS call and return cursor_expired immediately instead of
        # burning the window -- is filed as nelix-1hy, not this slice (3c.4 proves the ROUTER-restart
        # story: the daemon here never restarts at all).

        # S2a.3: Archive arm setup. Determine the archive component from the decoded cursor.
        archive_seq = None
        if self._store is not None:
            arch_pos = cursor.archive_position
            if arch_pos is not None:
                arch_epoch, arch_seq_val = arch_pos
                if arch_epoch != self._archive_epoch:
                    # Router restarted: a fresh archive_epoch means the old archive_seq is
                    # meaningless. Resync like the generation epoch mismatch.
                    return 200, {"event": None, "cursor_expired": True}
                archive_seq = arch_seq_val
            else:
                # Fresh cursor (no archive component): "start from now" -- read current board_seq
                # so only a NEW mutation wakes. Never deliver backlog.
                try:
                    board_seq = self._store.get_owner_board_seq(owner_id)
                except (sqlite3.Error, OSError):
                    pass
                except NelixError as e:
                    if e.code != STORE_UNAVAILABLE:
                        raise
                else:
                    cursor = cursor.advance_archive(self._archive_epoch, board_seq)
                    archive_seq = board_seq

        # S2a.3: Bounded multiplex -- poll archive board_seq + short-generation forward loop.
        # When both store and archive_seq are available, poll the store between short generation
        # waits so an archived mutation (ack, expiry, prune) can wake the wait mid-window.
        # The effective wait window stays ~25s (matching today's single forward), but each daemon
        # forward uses a short bounded timeout so we can interleave archive polling. No orphaned
        # 25s request.
        if self._store is not None and archive_seq is not None:
            start_time = time.time()

            while True:
                # Archive poll: check if board_seq advanced past the cursor's archive position.
                try:
                    current_seq = self._store.get_owner_board_seq(owner_id)
                except (sqlite3.Error, OSError):
                    pass
                except NelixError as e:
                    if e.code != STORE_UNAVAILABLE:
                        raise
                else:
                    if current_seq > archive_seq:
                        advanced = cursor.advance_archive(self._archive_epoch, current_seq)
                        return 200, {"event": {"kind": "archive"}, "cursor": encode(advanced)}

                elapsed = time.time() - start_time
                remaining = self._WAIT_WINDOW - elapsed
                if remaining <= 0:
                    return 200, {"event": None, "cursor": encode(cursor)}

                bounded = self._POLL_INTERVAL if self._POLL_INTERVAL < remaining else remaining
                try:
                    status, resp = self._wait_on_generation(
                        gen, owner_id, gen_sessions, after_seq, cursor, timeout=bounded)
                except NelixError as e:
                    if e.code != GENERATION_UNAVAILABLE:
                        raise
                    # Transient generation failure: continue polling archive
                    continue

                if resp.get("empty_orchestration") or resp.get("unownable") or resp.get("cursor_expired"):
                    return status, resp

                if resp.get("event") is not None:
                    return status, resp

                # Brief yield so a rapid fake-Backend loop does not overwhelm the server.
                time.sleep(0)
        else:
            # No archive arm: single generation forward with no bounded timeout,
            # byte-for-byte identical to the pre-S2a.3 behavior.
            return self._wait_on_generation(gen, owner_id, gen_sessions, after_seq, cursor)

    def _current_seq(self, gen, owner_id) -> int:
        """The generation's CURRENT int cursor (its board-wide latest_seq), for a "start from now"
        arm. Reads it the same phase-split, relay-mapped way every other forward does; an
        unavailable or unhealthy-shaped reply is a retryable GENERATION_UNAVAILABLE, never a crash."""
        client = RpcClient(gen.transport, owner_id)
        path = "/status?" + urllib.parse.urlencode({"owner_id": owner_id})
        status, body = relay(lambda: client.forward_raw("GET", path, None))
        if status == 200 and isinstance(body, dict) and _healthy_int_cursor(body.get("cursor")):
            return body["cursor"]
        raise NelixError(GENERATION_UNAVAILABLE,
                         "could not read the generation's current cursor to start the wait")

    def _wait_on_generation(self, gen, owner_id, sessions, after_seq, cursor,
                           timeout=None) -> "tuple[int, dict]":
        """Forward the daemon's MULTI-SESSION /wait for this generation's session subset, then map
        its reply back onto the vector cursor. The daemon owner-gates each sid (owner passed
        through), so the router never re-implements ownership.

        `timeout` is an optional bounded timeout (seconds) forwarded as a query param to the
        daemon's /wait, enabling the S2a.3 bounded multiplex."""
        client = RpcClient(gen.transport, owner_id)
        # Repeated session_id= params -> the daemon's multi-session wait. doseq=True encodes the list.
        params = [("owner_id", owner_id), ("after_seq", after_seq)]
        if timeout is not None:
            params.append(("timeout", str(timeout)))
        params += [("session_id", s) for s in sessions]
        path = "/wait?" + urllib.parse.urlencode(params)
        status, body = relay(lambda: client.forward_raw("GET", path, None))

        if status == 404:
            # The daemon owner-gated EVERY sid away (all foreign/unknown per owner.json): a wait
            # that can NEVER wake. An explicit signal, never a null the caller would re-issue forever.
            return 200, {"event": None, "unownable": True}
        if status != 200 or not isinstance(body, dict):
            raise NelixError(GENERATION_UNAVAILABLE,
                             f"generation /wait answered {status}, not a waitable reply")
        if body.get("cursor_expired"):
            # The daemon's ring dropped an event this cursor needed -> resync. Do NOT advance.
            return 200, {"event": None, "cursor_expired": True}

        evt = body.get("event")
        if evt is None:
            # Timeout: nothing new. Return the UNCHANGED cursor so the caller re-arms from here.
            return 200, {"event": None, "cursor": encode(cursor)}

        seq = evt.get("seq") if isinstance(evt, dict) else None
        if not _healthy_int_cursor(seq):
            raise NelixError(GENERATION_UNAVAILABLE,
                             "generation /wait event carried no usable seq")
        # An event -> advance ONLY this generation's component (spec §4) and return it + the token.
        advanced = cursor.advance(gen.generation_id, gen.epoch, seq)
        return 200, {"event": evt, "cursor": encode(advanced)}
