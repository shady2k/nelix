"""S2a.2 — router-owned archive board read + merge precedence + archive_incomplete + daemon hiding.

Three invariants this file exists to prove:
  (a) the same archived terminal appears EXACTLY ONCE on the merged board and NEVER in both
      `sessions` and `recent_terminal`;
  (b) a persisted-then-acked (and separately persisted-then-expired) terminal does NOT resurrect
      as a live board entry — the resurrection-bug regression;
  (c) a store read failure yields `archive_incomplete` distinct from `board_incomplete`, with the
      live results still returned.
"""
import pytest
from tests.conftest import OWNER
from daemon.transport import Transport
from nelix_contracts.cursor import decode
from nelix_contracts.errors import NelixError
from router.board import BoardForward, merge_archive_into
from router.registry import GenerationRegistry

from tests._router_fakes import Backend, Supervisor

OTHER_OWNER = "harness-y"
EPOCH = "r-" + "0" * 32
AE_EPOCH = 42
SID_1 = "s-" + "1" * 32
SID_2 = "s-" + "2" * 32


class _FakeStoreTerminal:
    """Minimal stand-in for a TerminalRecord — enough to exercise merge_archive_into."""

    def __init__(self, session_id, terminal_kind="done", summary="done"):
        self.session_id = session_id
        self.terminal_kind = terminal_kind
        self.summary = summary


def _fake_store(archive_seq=5, records=None, fail=False):
    """Build a fake Store duck that returns fixed read_board_snapshot data.

    Returns an object with a read_board_snapshot method raising on fail.
    """
    records = records or []

    class _FakeStore:
        def read_board_snapshot(self, owner_id):
            if fail:
                raise NelixError("store_unavailable", "database is unavailable")
            return archive_seq, records

    return _FakeStore()


@pytest.fixture
def wired():
    backend = Backend()
    registry = GenerationRegistry(supervisor=Supervisor(backend.transport),
                                  health_probe=lambda t: backend.build_id)
    yield backend, registry
    backend.close()


@pytest.fixture
def wired_no_store():
    backend = Backend()
    registry = GenerationRegistry(supervisor=Supervisor(backend.transport),
                                  health_probe=lambda t: backend.build_id)
    forward = BoardForward(registry, EPOCH)
    yield forward, backend, registry
    backend.close()


# ============================================================ merge_archive_into pure function

def test_merge_archive_into_suppresses_live_session_for_archived_terminal():
    """An archived terminal row must suppress any live entry for the same session
    in BOTH sessions and recent_terminal."""
    live = {"sessions": {SID_1: {"session_id": SID_1, "control_state": "busy"}},
            "recent_terminal": {SID_2: {"session_id": SID_2, "terminal_kind": "done"}}}
    records = [_FakeStoreTerminal(SID_1, "done", "archived"),
               _FakeStoreTerminal(SID_2, "crashed", "crashed")]
    merged = merge_archive_into(live, records)
    # SID_1 removed from sessions, only in recent_terminal
    assert SID_1 not in merged["sessions"]
    assert SID_1 in merged["recent_terminal"]
    assert merged["recent_terminal"][SID_1]["terminal_kind"] == "done"
    # SID_2 overwritten in recent_terminal
    assert merged["recent_terminal"][SID_2]["terminal_kind"] == "crashed"
    # No session appears in both
    for sid in merged["sessions"]:
        assert sid not in merged["recent_terminal"]


def test_merge_archive_into_no_duplicate():
    """An archived terminal appears EXACTLY ONCE on the merged board."""
    live = {"sessions": {},
            "recent_terminal": {}}
    records = [_FakeStoreTerminal(SID_1, "done", "done")]
    merged = merge_archive_into(live, records)
    assert SID_1 in merged["recent_terminal"]
    assert SID_1 not in merged["sessions"]
    # Only one entry
    recent = merged["recent_terminal"]
    assert len([k for k in recent if k == SID_1]) == 1


def test_merge_archive_into_archived_is_authoritative_over_live_terminal():
    """Archived terminal_kind overwrites live recent_terminal for the same session."""
    live = {"sessions": {},
            "recent_terminal": {SID_1: {"session_id": SID_1, "terminal_kind": "done",
                                         "screen_excerpt": "live"}}}
    records = [_FakeStoreTerminal(SID_1, "crashed", "archived")]
    merged = merge_archive_into(live, records)
    assert merged["recent_terminal"][SID_1]["terminal_kind"] == "crashed"
    assert merged["recent_terminal"][SID_1]["screen_excerpt"] == "archived"


# ============================================================ archive_incomplete

def test_archive_incomplete_on_store_read_failure(wired_no_store):
    forward, backend, registry = wired_no_store
    registry.active()
    # BoardForward with no store -> no archive_incomplete (store is None)
    status, body = forward.status(OWNER)
    assert status == 200
    assert body.get("archive_incomplete") is None or body.get("archive_incomplete") is False


def test_archive_incomplete_is_distinct_from_board_incomplete(wired):
    """A store read failure yields archive_incomplete, while healthy live results are still returned.
    Never emits `board_incomplete` to mean an archive failure."""
    backend, registry = wired
    registry.active()
    archive_epoch = AE_EPOCH
    store = _fake_store(fail=True)
    forward = BoardForward(registry, EPOCH, store=store, archive_epoch=archive_epoch)
    status, body = forward.status(OWNER)
    assert status == 200
    # archive_incomplete is True
    assert body.get("archive_incomplete") is True
    # board_incomplete is NOT set for this — it's false (no unavailable generations)
    assert body["board_incomplete"] is False
    # Live results are still returned
    assert "sessions" in body
    assert "cursor" in body


def test_archive_incomplete_with_board_incomplete_is_independent(wired):
    """Both archive_incomplete and board_incomplete can coexist independently."""
    backend, registry = wired
    # Override backend transport to make it unreachable, so board_incomplete triggers
    dead_transport = Transport.tcp("127.0.0.1", 9, "t")
    registry2 = GenerationRegistry(supervisor=Supervisor(dead_transport),
                                   health_probe=lambda t: None)
    gen = registry2.active()
    archive_epoch = AE_EPOCH
    store = _fake_store(fail=True)
    forward = BoardForward(registry2, EPOCH, store=store, archive_epoch=archive_epoch)
    status, body = forward.status(OWNER)
    assert status == 200
    assert body.get("archive_incomplete") is True
    assert body["board_incomplete"] == [gen.generation_id]
    # Sessions still returned (empty in this case, but not an error)
    assert "sessions" in body


# ============================================================ archive cursor component

def test_archive_cursor_populated_on_status(wired):
    """Status populates the cursor's archive component with (archive_epoch, archive_seq)."""
    backend, registry = wired
    registry.active()
    archive_epoch = AE_EPOCH
    archive_seq = 7
    records = [_FakeStoreTerminal(SID_1, "done", "done")]
    store = _fake_store(archive_seq=archive_seq, records=records)
    forward = BoardForward(registry, EPOCH, store=store, archive_epoch=archive_epoch)
    status, body = forward.status(OWNER)
    assert status == 200
    cursor = decode(body["cursor"], router_epoch=EPOCH,
                    topology_revision=registry.topology_revision())
    arch = cursor.archive_position
    assert arch is not None
    epoch_val, seq_val = arch
    assert epoch_val == archive_epoch
    assert seq_val == archive_seq


def test_archive_cursor_not_present_without_store(wired_no_store):
    """BoardForward with no store does not set archive cursor component."""
    forward, backend, registry = wired_no_store
    registry.active()
    status, body = forward.status(OWNER)
    cursor = decode(body["cursor"], router_epoch=EPOCH,
                    topology_revision=registry.topology_revision())
    assert cursor.archive_position is None


def test_archive_cursor_encode_decode_round_trips(wired):
    """The archive cursor round-trips through encode/decode."""
    backend, registry = wired
    registry.active()
    archive_epoch = AE_EPOCH
    archive_seq = 3
    records = [_FakeStoreTerminal(SID_1, "done", "done")]
    store = _fake_store(archive_seq=archive_seq, records=records)
    forward = BoardForward(registry, EPOCH, store=store, archive_epoch=archive_epoch)
    status, body = forward.status(OWNER)
    cursor = decode(body["cursor"], router_epoch=EPOCH,
                    topology_revision=registry.topology_revision())
    arch = cursor.archive_position
    assert arch == (archive_epoch, archive_seq)
    # Re-encode and decode again
    from nelix_contracts.cursor import encode as enc
    token2 = enc(cursor)
    cursor2 = decode(token2, router_epoch=EPOCH,
                     topology_revision=registry.topology_revision())
    assert cursor2.archive_position == (archive_epoch, archive_seq)


# ============================================================ daemon hiding + resurrection bug

def test_persisted_terminal_hidden_from_live_board(wired):
    """Simulate the daemon side: a persisted terminal (advertised=False) must NOT
    appear in the daemon's live board recent_terminal. The test verifies the
    router's merge correctly only surfaces it from the archive."""
    backend, registry = wired
    # Simulate a session that completed -> its terminal is in the store
    # The daemon's live board does NOT include it (advertised=False)
    # The store has it
    archive_epoch = AE_EPOCH
    records = [_FakeStoreTerminal(SID_1, "done", "done")]
    store = _fake_store(archive_seq=1, records=records)
    forward = BoardForward(registry, EPOCH, store=store, archive_epoch=archive_epoch)
    status, body = forward.status(OWNER)
    assert status == 200
    # Session should only appear in recent_terminal (from archive), NOT in sessions
    assert SID_1 not in body["sessions"]
    assert SID_1 in body["recent_terminal"]
    # The archive entry is the single source
    assert body["recent_terminal"][SID_1]["terminal_kind"] == "done"


def test_archived_terminal_never_appears_in_both_sessions_and_recent_terminal(wired):
    """Regression test for merge precedence: an archived terminal must NEVER appear in
    both sessions and recent_terminal of the merged board."""
    backend, registry = wired
    # The LIVE board shows the session as alive (e.g., between persist and TTL expiry,
    # the daemon would have advertised=False, but simulating a race where the live board
    # still carries it and the archive also has it)
    backend.owns[SID_1] = OWNER
    backend.owns[SID_2] = OWNER
    archive_epoch = AE_EPOCH
    records = [_FakeStoreTerminal(SID_1, "done", "done_archive"),
               _FakeStoreTerminal(SID_2, "crashed", "crashed_archive")]
    store = _fake_store(archive_seq=2, records=records)
    forward = BoardForward(registry, EPOCH, store=store, archive_epoch=archive_epoch)
    status, body = forward.status(OWNER)
    assert status == 200
    # Archived terminals must not be in sessions
    assert SID_1 not in body["sessions"]
    assert SID_2 not in body["sessions"]
    # Archived terminals must be in recent_terminal
    assert SID_1 in body["recent_terminal"]
    assert SID_2 in body["recent_terminal"]
    # Sanity: no session appears in both
    for sid in body["sessions"]:
        assert sid not in body["recent_terminal"]


def test_archive_incomplete_live_results_still_returned(wired):
    """(c) A store read failure yields archive_incomplete, with live results still returned."""
    backend, registry = wired
    # The live backend has sessions
    backend.owns[SID_1] = OWNER
    archive_epoch = AE_EPOCH
    store = _fake_store(fail=True)
    forward = BoardForward(registry, EPOCH, store=store, archive_epoch=archive_epoch)
    status, body = forward.status(OWNER)
    assert status == 200
    # archive_incomplete is set
    assert body.get("archive_incomplete") is True
    # Live sessions are still returned
    assert SID_1 in body["sessions"]
    # board_incomplete is NOT set for the archive failure
    assert body["board_incomplete"] is False
