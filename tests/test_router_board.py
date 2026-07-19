"""nelix-3rm slice 3c.3a: the FAN-OUT board -- router GET /status with NO session_id.

The daemon's board-wide /status (`daemon/manager.py::status(session_id=None)`) is already
OWNER-FILTERED and already carries a GLOBAL int cursor (`EventQueue.latest_seq()`). This slice's
job is router-side: forward that per-generation board to every tracked generation (N=1 today),
MERGE the results into one router-owned envelope (a real N-way union, Plan-4-ready), and attach an
opaque VECTOR CURSOR (`nelix_contracts.cursor`) whose per-generation component is keyed on that
generation's STABLE `slot_id` (`router/registry.py`) with the value (the generation's epoch, that
generation's own int cursor) -- never a hand-rolled cursor, never a second error-mapping (reuses
`router/forwarding.relay`).

Three invariants this file exists to prove:
  * the board stays OWNER-FILTERED all the way through the router merge (no cross-owner leak);
  * an UNAVAILABLE generation is never silently dropped -- it is named in an explicit, retryable
    `board_incomplete` marker, and the route still answers 200 with whatever healthy generations
    produced (spec §4);
  * an empty registry does not lie "honestly empty" when a daemon is already running (fix-pass
    finding #3), and a malformed-but-200 generation reply is never merged as healthy nor allowed to
    hard-error the whole board (fix-pass finding #2).

/wait (cursor DECODE + long-poll + expiry) is 3c.3b, not this slice -- this file only proves the
board is CONSTRUCTED and the cursor is ENCODED.
"""
import pytest

from tests.conftest import OWNER
from daemon.transport import Transport
from nelix_contracts.cursor import decode
from nelix_contracts.errors import NelixError
from router.board import BoardForward, merge_boards
from router.registry import GenerationRegistry

from tests._router_fakes import Backend, Supervisor

OTHER_OWNER = "harness-y"
EPOCH = "r-" + "0" * 32
SID_1 = "s-" + "1" * 32
SID_2 = "s-" + "2" * 32


class _NoDaemonSupervisor:
    """A supervisor whose singleton lock is held by NOBODY -- the non-spawning discovery probe
    (fix-pass finding #3) must find nothing and leave the board honestly empty. Deliberately has no
    `active_generation`/`ensure_running`: a board read must never reach for either (it must never
    take the spawning path), so a fake missing them entirely proves the board never even tries."""

    def held_generation(self):
        return None


@pytest.fixture
def wired():
    backend = Backend()
    registry = GenerationRegistry(supervisor=Supervisor(backend.transport),
                                  health_probe=lambda t: backend.build_id)
    forward = BoardForward(registry, EPOCH)
    yield forward, backend, registry
    backend.close()


# ============================================================ merge_boards: pure N-way union

def test_merge_boards_unions_sessions_and_recent_terminal_across_generations():
    # Plan-4-ready: TWO generations' boards merge into one -- this is the real union that must
    # never collapse to "just return the one board" even though N=1 today.
    board_a = {"sessions": {"s-a": {"v": 1}}, "recent_terminal": {"t-a": {"v": 1}}, "cursor": 3}
    board_b = {"sessions": {"s-b": {"v": 2}}, "recent_terminal": {"t-b": {"v": 2}}, "cursor": 7}
    merged = merge_boards([("gen-a", board_a), ("gen-b", board_b)])
    assert merged["sessions"] == {"s-a": {"v": 1}, "s-b": {"v": 2}}
    assert merged["recent_terminal"] == {"t-a": {"v": 1}, "t-b": {"v": 2}}


def test_merge_boards_of_one_generation_collapses_to_its_board():
    board = {"sessions": {"s-a": {"v": 1}}, "recent_terminal": {}, "cursor": 3}
    merged = merge_boards([("gen-a", board)])
    assert merged["sessions"] == {"s-a": {"v": 1}}


def test_merge_boards_of_no_generations_is_an_empty_board():
    assert merge_boards([]) == {"sessions": {}, "recent_terminal": {}}


# ============================================================ owner filtering survives the merge

def test_sessions_are_filtered_to_the_requesting_owner(wired):
    forward, backend, registry = wired
    registry.active()                          # observe the one generation once
    backend.owns[SID_1] = OWNER
    backend.owns[SID_2] = OTHER_OWNER
    status, body = forward.status(OWNER)
    assert status == 200
    assert list(body["sessions"].keys()) == [SID_1]


def test_recent_terminal_is_filtered_to_the_requesting_owner(wired):
    forward, backend, registry = wired
    registry.active()
    backend.recent_terminal = {SID_1: {"session_id": SID_1, "terminal_kind": "done"},
                               SID_2: {"session_id": SID_2, "terminal_kind": "done"}}
    backend.recent_terminal_owner = {SID_1: OWNER, SID_2: OTHER_OWNER}
    status, body = forward.status(OWNER)
    assert list(body["recent_terminal"].keys()) == [SID_1]


# ============================================================ the vector cursor

def test_cursor_component_is_keyed_on_slot_id_with_value_epoch_and_the_daemons_int_cursor(wired):
    # fix-pass finding #1: the map KEY is the STABLE slot_id, not the volatile per-incarnation
    # epoch -- the epoch is carried as part of the VALUE, alongside the daemon's own int cursor.
    forward, backend, registry = wired
    gen = registry.active()
    backend.board_cursor = 42
    status, body = forward.status(OWNER)
    assert status == 200
    cursor = decode(body["cursor"], router_epoch=EPOCH, topology_revision=registry.topology_revision())
    assert cursor.position_for(gen.slot_id) == (gen.epoch, 42)


def test_cursor_key_survives_a_daemon_restart_new_epoch_same_slot():
    # fix-pass finding #1's core scenario: a daemon restart (same registry slot, new epoch) must
    # NOT orphan the caller's prior cursor position -- position_for(slot_id) must keep resolving to
    # the SAME component, with only the epoch VALUE changing to the new incarnation's.
    backend = Backend()
    try:
        sup = Supervisor(backend.transport)
        registry = GenerationRegistry(supervisor=sup, health_probe=lambda t: backend.build_id)
        forward = BoardForward(registry, EPOCH)
        gen_before = registry.active()
        backend.board_cursor = 5
        _, body_before = forward.status(OWNER)
        cursor_before = decode(body_before["cursor"], router_epoch=EPOCH,
                               topology_revision=registry.topology_revision())
        assert cursor_before.position_for(gen_before.slot_id) == (gen_before.epoch, 5)

        sup.inc = {"pid": 999, "start_fingerprint": "fp-restarted"}   # simulate a daemon restart
        gen_after = registry.active()
        assert gen_after.slot_id == gen_before.slot_id          # same registry slot
        assert gen_after.epoch != gen_before.epoch              # a fresh incarnation minted a fresh epoch
        backend.board_cursor = 1
        _, body_after = forward.status(OWNER)
        cursor_after = decode(body_after["cursor"], router_epoch=EPOCH,
                              topology_revision=registry.topology_revision())
        # position_for(slot_id) resolves to the SAME component, key never orphaned by the restart --
        # exactly the shape 3c.3b's /wait needs to detect "same generation, new epoch" (CURSOR_EXPIRED
        # for that component) rather than seeing a whole new, unrelated map entry.
        assert cursor_after.position_for(gen_after.slot_id) == (gen_after.epoch, 1)
    finally:
        backend.close()


def test_cursor_carries_the_router_epoch_and_topology_revision(wired):
    forward, backend, registry = wired
    registry.active()
    status, body = forward.status(OWNER)
    cursor = decode(body["cursor"], router_epoch=EPOCH, topology_revision=registry.topology_revision())
    assert cursor.router_epoch == EPOCH
    assert cursor.topology_revision == registry.topology_revision()


def test_router_epoch_is_stable_across_repeated_board_reads(wired):
    # A stale/drifting router_epoch between two reads would make the SECOND token fail to decode
    # against the SAME router_epoch (CURSOR_EXPIRED) -- decoding both proves it never moved.
    forward, backend, registry = wired
    registry.active()
    _, body1 = forward.status(OWNER)
    _, body2 = forward.status(OWNER)
    decode(body1["cursor"], router_epoch=EPOCH, topology_revision=registry.topology_revision())
    decode(body2["cursor"], router_epoch=EPOCH, topology_revision=registry.topology_revision())


# ============================================================ board_incomplete

def test_board_incomplete_is_false_when_every_generation_is_healthy(wired):
    forward, backend, registry = wired
    registry.active()
    status, body = forward.status(OWNER)
    assert status == 200
    assert body["board_incomplete"] is False


def test_board_incomplete_names_the_unavailable_generation_but_still_returns_200():
    # spec §4: never silently omit a down generation's sessions -- explicit, retryable
    # board_incomplete, while still answering 200 with whatever healthy generations produced
    # (none, at N=1).
    class _DeadSupervisor:
        _t = Transport.tcp("127.0.0.1", 9, "t")            # discard port: connection refused

        def active_generation(self):
            return (self._t, {"pid": 1, "start_fingerprint": "fp"})

        def held_generation(self):
            return (self._t, {"pid": 1, "start_fingerprint": "fp"})

        def ensure_running(self):
            return self._t

    registry = GenerationRegistry(supervisor=_DeadSupervisor(), health_probe=lambda t: None)
    gen = registry.active()                     # observe the (unreachable) generation once
    forward = BoardForward(registry, EPOCH)
    status, body = forward.status(OWNER)
    assert status == 200
    assert body["board_incomplete"] == [gen.slot_id]
    assert body["sessions"] == {}
    assert body["recent_terminal"] == {}
    # The token is still a real, decodable cursor -- an incomplete board is not a broken one.
    cursor = decode(body["cursor"], router_epoch=EPOCH, topology_revision=registry.topology_revision())
    assert cursor.position_for(gen.slot_id) is None           # never advanced: it never answered


def test_empty_registry_is_an_honestly_empty_board_not_incomplete():
    # No daemon holds the singleton lock -- there is truly nothing to discover (fix-pass finding
    # #3) and no generation to be "unavailable", so this is a genuinely empty board, never a
    # masked failure.
    registry = GenerationRegistry(supervisor=_NoDaemonSupervisor())
    forward = BoardForward(registry, EPOCH)
    status, body = forward.status(OWNER)
    assert status == 200
    assert body == {"sessions": {}, "recent_terminal": {},
                    "cursor": body["cursor"], "board_incomplete": False}
    cursor = decode(body["cursor"], router_epoch=EPOCH, topology_revision=registry.topology_revision())
    assert dict(cursor.positions) == {}


def test_board_read_never_spawns_a_generation():
    # Mirrors /health, /capabilities, /generation_list: a board read is a "read-only" probe and
    # must not subprocess.Popen a daemon as a side effect. registry.generations(discover=True) is
    # the non-spawning snapshot+discovery the board uses; discovery MAY call held_generation() (a
    # cheap lock-file read, no RPC, no spawn -- fix-pass finding #3), but must NEVER reach for
    # active_generation()/ensure_running(), either of which can spawn.
    class _BoomIfSpawned:
        def active_generation(self):
            raise AssertionError("board read must never probe to spawn a generation")

        def ensure_running(self):
            raise AssertionError("board read must never spawn a generation")

        def held_generation(self):
            return None                  # non-spawning discovery: no daemon currently running

    registry = GenerationRegistry(supervisor=_BoomIfSpawned())
    forward = BoardForward(registry, EPOCH)
    status, body = forward.status(OWNER)
    assert status == 200
    assert body["sessions"] == {}
    assert body["board_incomplete"] is False


# ==================================== nelix-3rm 3c.3a fix-pass finding #3: empty-registry discovery

def test_a_fresh_registry_discovers_a_surviving_daemon_via_held_generation():
    # A router restart empties the registry, but a router restart kills nothing -- the daemon's
    # sessions SURVIVE it. The FIRST board read after that restart must not lie "empty": it
    # discovers the currently-running daemon via the non-spawning held_generation() read (the same
    # authoritative lock-holder read active() uses under its lock) and includes its sessions.
    backend = Backend()
    try:
        registry = GenerationRegistry(supervisor=Supervisor(backend.transport),
                                      health_probe=lambda t: backend.build_id)
        backend.owns[SID_1] = OWNER                     # a session the "surviving" daemon reports
        forward = BoardForward(registry, EPOCH)
        status, body = forward.status(OWNER)             # registry.active() never called
        assert status == 200
        assert body["board_incomplete"] is False
        assert SID_1 in body["sessions"]
    finally:
        backend.close()


def test_discovery_never_spawns_even_when_a_daemon_is_found():
    # The daemon IS running (held_generation() reports it) -- discovery must still never reach for
    # the spawning path (active_generation()/ensure_running()), only the cheap lock-holder read.
    backend = Backend()
    try:
        class _BoomIfEnsurePathTaken:
            def __init__(self, transport):
                self._t = transport

            def held_generation(self):
                return (self._t, {"pid": 1, "start_fingerprint": "fp"})

            def active_generation(self):
                raise AssertionError("discovery must never take the spawning ensure path")

            def ensure_running(self):
                raise AssertionError("discovery must never spawn a generation")

        registry = GenerationRegistry(supervisor=_BoomIfEnsurePathTaken(backend.transport),
                                      health_probe=lambda t: backend.build_id)
        forward = BoardForward(registry, EPOCH)
        status, body = forward.status(OWNER)
        assert status == 200
        assert body["board_incomplete"] is False
    finally:
        backend.close()


def test_no_running_daemon_after_discovery_is_still_honestly_empty():
    registry = GenerationRegistry(supervisor=_NoDaemonSupervisor())
    forward = BoardForward(registry, EPOCH)
    status, body = forward.status(OWNER)
    assert status == 200
    assert body["board_incomplete"] is False
    assert body["sessions"] == {}


# ======================= nelix-3rm 3c.3a fix-pass finding #2: a malformed-but-200 generation reply

@pytest.mark.parametrize("bad_cursor", [-1, "abc", True], ids=["negative", "non_int", "bool"])
def test_a_malformed_cursor_in_an_otherwise_200_reply_is_incomplete_not_merged_not_a_hard_error(
        wired, bad_cursor):
    forward, backend, registry = wired
    gen = registry.active()
    backend.board_cursor = bad_cursor
    status, body = forward.status(OWNER)                 # must not raise -- never a hard error
    assert status == 200
    assert body["board_incomplete"] == [gen.slot_id]      # unavailable, not silently merged empty
    assert body["sessions"] == {}
    assert body["recent_terminal"] == {}
    # The cursor is still real and decodable; this generation's component never advanced.
    cursor = decode(body["cursor"], router_epoch=EPOCH, topology_revision=registry.topology_revision())
    assert cursor.position_for(gen.slot_id) is None


def test_a_non_dict_sessions_field_in_an_otherwise_200_reply_is_incomplete_not_merged(wired):
    forward, backend, registry = wired
    gen = registry.active()
    backend.board_reply_override = {"cursor": 3, "sessions": "oops", "recent_terminal": {}}
    status, body = forward.status(OWNER)
    assert status == 200
    assert body["board_incomplete"] == [gen.slot_id]
    assert body["sessions"] == {}


def test_a_non_dict_recent_terminal_field_in_an_otherwise_200_reply_is_incomplete_not_merged(wired):
    forward, backend, registry = wired
    gen = registry.active()
    backend.board_reply_override = {"cursor": 3, "sessions": {}, "recent_terminal": ["oops"]}
    status, body = forward.status(OWNER)
    assert status == 200
    assert body["board_incomplete"] == [gen.slot_id]


def test_a_200_reply_missing_sessions_and_recent_terminal_entirely_is_incomplete_not_merged_empty(
        wired):
    # {"cursor": 12} alone is a dict containing "cursor" -- the OLD guard's whole check -- but it
    # is not a healthy board shape and must not be silently merged as an empty-but-healthy
    # generation (the exact silent omission spec §4 forbids).
    forward, backend, registry = wired
    gen = registry.active()
    backend.board_reply_override = {"cursor": 12}
    status, body = forward.status(OWNER)
    assert status == 200
    assert body["board_incomplete"] == [gen.slot_id]
    assert body["sessions"] == {}


# ============================================================ owner_id shape validation

def test_bad_owner_id_shape_is_invalid_request(wired):
    forward, backend, registry = wired
    with pytest.raises(NelixError) as exc:
        forward.status("has space")
    assert exc.value.code == "invalid_request"
