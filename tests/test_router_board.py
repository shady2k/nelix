"""nelix-3rm slice 3c.3a: the FAN-OUT board -- router GET /status with NO session_id.

The daemon's board-wide /status (`daemon/manager.py::status(session_id=None)`) is already
OWNER-FILTERED and already carries a GLOBAL int cursor (`EventQueue.latest_seq()`). This slice's
job is router-side: forward that per-generation board to every tracked generation (N=1 today),
MERGE the results into one router-owned envelope (a real N-way union, Plan-4-ready), and attach an
opaque VECTOR CURSOR (`nelix_contracts.cursor`) whose per-generation component is (the
generation's epoch, that generation's own int cursor) -- never a hand-rolled cursor, never a
second error-mapping (reuses `router/forwarding.relay`).

Two invariants this file exists to prove:
  * the board stays OWNER-FILTERED all the way through the router merge (no cross-owner leak);
  * an UNAVAILABLE generation is never silently dropped -- it is named in an explicit, retryable
    `board_incomplete` marker, and the route still answers 200 with whatever healthy generations
    produced (spec §4).

/wait (cursor DECODE + long-poll + expiry) is 3c.3b, not this slice -- this file only proves the
board is CONSTRUCTED and the cursor is ENCODED.
"""
import pytest

from conftest import OWNER
from daemon.transport import Transport
from nelix_contracts.cursor import decode
from nelix_contracts.errors import NelixError
from router.board import BoardForward, merge_boards
from router.registry import GenerationRegistry

from _router_fakes import Backend, Supervisor

OTHER_OWNER = "harness-y"
EPOCH = "r-" + "0" * 32
SID_1 = "s-" + "1" * 32
SID_2 = "s-" + "2" * 32


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

def test_cursor_component_is_the_generations_epoch_and_the_daemons_int_cursor(wired):
    forward, backend, registry = wired
    gen = registry.active()
    backend.board_cursor = 42
    status, body = forward.status(OWNER)
    assert status == 200
    cursor = decode(body["cursor"], router_epoch=EPOCH, topology_revision=registry.topology_revision())
    assert cursor.position_for(gen.epoch) == (gen.epoch, 42)


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
    assert body["board_incomplete"] == [gen.epoch]
    assert body["sessions"] == {}
    assert body["recent_terminal"] == {}
    # The token is still a real, decodable cursor -- an incomplete board is not a broken one.
    cursor = decode(body["cursor"], router_epoch=EPOCH, topology_revision=registry.topology_revision())
    assert cursor.position_for(gen.epoch) is None            # never advanced: it never answered


def test_empty_registry_is_an_honestly_empty_board_not_incomplete():
    # Nothing has ever been observed (no /start, no /health) -- there is no generation to be
    # "unavailable", so this is a genuinely empty board, not a masked failure.
    registry = GenerationRegistry(supervisor=object())
    forward = BoardForward(registry, EPOCH)
    status, body = forward.status(OWNER)
    assert status == 200
    assert body == {"sessions": {}, "recent_terminal": {},
                    "cursor": body["cursor"], "board_incomplete": False}
    cursor = decode(body["cursor"], router_epoch=EPOCH, topology_revision=registry.topology_revision())
    assert dict(cursor.positions) == {}


def test_board_read_never_spawns_a_generation():
    # Mirrors /health, /capabilities, /generation_list: a board read is a "read-only" probe and
    # must not subprocess.Popen a daemon as a side effect. registry.generations() (never .active())
    # is the non-spawning snapshot every other read route already uses.
    class _BoomSupervisor:
        def active_generation(self):
            raise AssertionError("board read must never probe to spawn a generation")

        def ensure_running(self):
            raise AssertionError("board read must never spawn a generation")

        def held_generation(self):
            raise AssertionError("board read must never touch the lock holder")

    registry = GenerationRegistry(supervisor=_BoomSupervisor())
    forward = BoardForward(registry, EPOCH)
    status, body = forward.status(OWNER)
    assert status == 200
    assert body["sessions"] == {}
    assert body["board_incomplete"] is False


# ============================================================ owner_id shape validation

def test_bad_owner_id_shape_is_invalid_request(wired):
    forward, backend, registry = wired
    with pytest.raises(NelixError) as exc:
        forward.status("has space")
    assert exc.value.code == "invalid_request"
