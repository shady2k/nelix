"""nelix-3rm slice 3c.3b: the router ORCHESTRATION /wait — one waiter for the N workers of an
orchestration, long-polling the generation(s) via the vector cursor (spec §1 fan-out, §4 cursors,
§10 "orchestration is the safe middle").

WaitForward turns (owner_id, orchestration_id, cursor) into a long-poll:
  * derive the orchestration's sessions from the owner-scoped StartLedger (empty -> an explicit
    no-wake signal, never a silent 25s null spin);
  * DECODE the vector cursor against the CURRENT router state (router-epoch mismatch -> CURSOR_EXPIRED;
    topology change -> BOARD_CHANGED — both explicit markers the caller resyncs on);
  * for the generation(s) (N=1): take the cursor's component for the generation's STABLE slot_id;
    if its epoch != the generation's CURRENT epoch (the daemon restarted, seqs reset) -> CURSOR_EXPIRED,
    else forward the daemon's MULTI-SESSION wait for (the orchestration's sessions, the component's seq);
  * on an event, advance ONLY that component and return {event, cursor}; on timeout, the unchanged
    cursor; on the generation's cursor_expired, the marker; on an all-unowned set, the unownable signal.

These are unit tests of WaitForward against the in-process Backend fake's daemon /wait shape; the
REAL daemon wake + the owner-gate the daemon (not the router) enforces live in
test_router_wait_realdaemon.py."""
import pytest

import paths
from conftest import OWNER
from nelix_contracts.cursor import decode, encode, new_cursor
from nelix_contracts.errors import NelixError
from nelix_store.ledger import StartLedger
from router.board import BoardForward
from router.registry import GenerationRegistry
from router.wait import WaitForward

from _router_fakes import Backend, Supervisor

OTHER_OWNER = "harness-y"
EPOCH = "r-" + "0" * 32
ORCH = "o-" + "1" * 32
FP = "fp"


@pytest.fixture
def wired():
    backend = Backend()
    sup = Supervisor(backend.transport)
    registry = GenerationRegistry(supervisor=sup, health_probe=lambda t: backend.build_id)
    ledger = StartLedger(paths.nelix_root())
    board = BoardForward(registry, EPOCH)
    wait = WaitForward(ledger, registry, EPOCH)
    yield wait, board, ledger, registry, backend, sup
    ledger.close()
    backend.close()


def _reserve(ledger, backend, owner, orch, key):
    """Reserve a session in the ledger AND record the backend as its owner (the daemon-side gate)."""
    sid = ledger.reserve(idempotency_key=key, owner_id=owner, orchestration_id=orch,
                         request_fingerprint=FP).session_id
    backend.owns[sid] = owner
    return sid


def _board_cursor(board, owner):
    _, body = board.status(owner)
    return body["cursor"]


def _slot_and_epoch(registry):
    g = registry.generations()[0]
    return g.slot_id, g.epoch


# ============================================================ the wake path

def test_wait_wakes_on_an_event_and_returns_the_advanced_cursor(wired):
    wait, board, ledger, registry, backend, sup = wired
    sid = _reserve(ledger, backend, OWNER, ORCH, "k1")
    backend.board_cursor = 0
    token = _board_cursor(board, OWNER)                 # cursor with the slot component at seq 0
    backend.wait_events[sid] = {"seq": 5, "session_id": sid, "kind": "waiting_for_user"}
    status, resp = wait.wait(OWNER, ORCH, token)
    assert status == 200
    assert resp["event"]["session_id"] == sid and resp["event"]["seq"] == 5
    slot_id, gen_epoch = _slot_and_epoch(registry)
    cursor = decode(resp["cursor"], router_epoch=EPOCH,
                    topology_revision=registry.topology_revision())
    assert cursor.position_for(slot_id) == (gen_epoch, 5)   # ONLY this component advanced, to the seq


def test_wait_forwards_the_orchestration_sessions_and_owner_to_the_generation(wired):
    wait, board, ledger, registry, backend, sup = wired
    s1 = _reserve(ledger, backend, OWNER, ORCH, "k1")
    s2 = _reserve(ledger, backend, OWNER, ORCH, "k2")
    backend.board_cursor = 0
    token = _board_cursor(board, OWNER)
    wait.wait(OWNER, ORCH, token)
    call = backend.wait_calls[-1]
    assert set(call["session_ids"]) == {s1, s2}         # the whole orchestration, forwarded as a set
    assert call["owner_id"] == OWNER                    # owner passed through (the daemon gates)
    assert call["after_seq"] == "0"                     # the cursor component's seq


def test_wait_timeout_returns_the_unchanged_cursor(wired):
    wait, board, ledger, registry, backend, sup = wired
    _reserve(ledger, backend, OWNER, ORCH, "k1")
    backend.board_cursor = 0
    token = _board_cursor(board, OWNER)                 # nothing primed -> the backend times out null
    status, resp = wait.wait(OWNER, ORCH, token)
    assert status == 200
    assert resp["event"] is None
    slot_id, gen_epoch = _slot_and_epoch(registry)
    cursor = decode(resp["cursor"], router_epoch=EPOCH,
                    topology_revision=registry.topology_revision())
    assert cursor.position_for(slot_id) == (gen_epoch, 0)   # unchanged: re-arm from the same spot


# ============================================================ the resync markers

def test_wait_router_epoch_mismatch_is_cursor_expired(wired):
    # A cursor minted against a DIFFERENT router epoch (the router restarted) -> the positions
    # describe a world that no longer exists. decode raises CURSOR_EXPIRED; the router returns the
    # explicit marker, never a silent stall.
    wait, board, ledger, registry, backend, sup = wired
    _reserve(ledger, backend, OWNER, ORCH, "k1")
    stale = encode(new_cursor("r-" + "9" * 32, registry.topology_revision()))
    status, resp = wait.wait(OWNER, ORCH, stale)
    assert status == 200
    assert resp["event"] is None and resp["cursor_expired"] is True


def test_wait_topology_change_is_board_changed(wired):
    wait, board, ledger, registry, backend, sup = wired
    _reserve(ledger, backend, OWNER, ORCH, "k1")
    # a token minted at a DIFFERENT topology revision (a generation appeared/retired).
    stale = encode(new_cursor(EPOCH, registry.topology_revision() + 1))
    status, resp = wait.wait(OWNER, ORCH, stale)
    assert status == 200
    assert resp["event"] is None and resp["board_changed"] is True


def test_wait_daemon_restart_epoch_mismatch_is_cursor_expired(wired):
    # The cursor decodes fine (same router epoch + topology), but its component's epoch is the OLD
    # incarnation's. A daemon restart minted a fresh epoch (seqs reset), so the stale epoch's seq is
    # meaningless -> CURSOR_EXPIRED, never a wait on a stale epoch's seq.
    wait, board, ledger, registry, backend, sup = wired
    _reserve(ledger, backend, OWNER, ORCH, "k1")
    backend.board_cursor = 3
    token = _board_cursor(board, OWNER)                 # component epoch = current incarnation's
    sup.inc = {"pid": 999, "start_fingerprint": "restarted"}   # simulate a daemon restart
    status, resp = wait.wait(OWNER, ORCH, token)
    assert status == 200
    assert resp["event"] is None and resp["cursor_expired"] is True


def test_wait_generation_cursor_expired_is_relayed(wired):
    wait, board, ledger, registry, backend, sup = wired
    _reserve(ledger, backend, OWNER, ORCH, "k1")
    backend.board_cursor = 0
    token = _board_cursor(board, OWNER)
    backend.wait_cursor_expired = True                  # the daemon's ring dropped a needed event
    status, resp = wait.wait(OWNER, ORCH, token)
    assert status == 200
    assert resp["event"] is None and resp["cursor_expired"] is True


# ============================================================ the no-wake signals

def test_wait_on_an_empty_orchestration_is_an_explicit_no_wake_signal(wired):
    # An orchestration with no waitable sessions can NEVER wake -> an explicit marker, never a
    # silent 25s null spin the caller would re-issue forever.
    wait, board, ledger, registry, backend, sup = wired
    status, resp = wait.wait(OWNER, ORCH, None)
    assert status == 200
    assert resp["event"] is None and resp["empty_orchestration"] is True


def test_wait_owner_cannot_derive_another_owners_orchestration_sessions(wired):
    # Owner isolation at the router/ledger seam: owner Y waiting on the SAME orchestration_id STRING
    # Z used sees NONE of Z's sessions (the ledger query filters on owner_id too) -> an empty
    # orchestration for Y, never Z's session forwarded to a wait.
    wait, board, ledger, registry, backend, sup = wired
    _reserve(ledger, backend, OTHER_OWNER, ORCH, "k-z")
    status, resp = wait.wait(OWNER, ORCH, None)
    assert status == 200
    assert resp["empty_orchestration"] is True
    assert backend.wait_calls == []                     # never even forwarded a wait to the generation


def test_wait_unownable_when_the_generation_gates_every_session_away(wired):
    # The ledger has a session for OWNER under ORCH, but owner.json (the daemon's authority)
    # disagrees -> the daemon /wait 404s the whole set -> the router returns an explicit unownable
    # signal, never a null spin.
    wait, board, ledger, registry, backend, sup = wired
    ledger.reserve(idempotency_key="k1", owner_id=OWNER, orchestration_id=ORCH,
                   request_fingerprint=FP)             # NOT recorded in backend.owns
    backend.board_cursor = 0
    token = _board_cursor(board, OWNER)
    status, resp = wait.wait(OWNER, ORCH, token)
    assert status == 200
    assert resp["event"] is None and resp["unownable"] is True


# ============================================================ start-from-now (missing cursor)

def test_wait_with_no_cursor_starts_from_the_current_position(wired):
    # No cursor = "start from now": the router reads the generation's CURRENT int cursor and arms
    # from there, so only a NEW event wakes it (never re-delivers old history).
    wait, board, ledger, registry, backend, sup = wired
    _reserve(ledger, backend, OWNER, ORCH, "k1")
    backend.board_cursor = 12                            # the generation's current position
    status, resp = wait.wait(OWNER, ORCH, None)
    assert status == 200
    call = backend.wait_calls[-1]
    assert call["after_seq"] == "12"                    # armed from now, not from 0
    slot_id, gen_epoch = _slot_and_epoch(registry)
    cursor = decode(resp["cursor"], router_epoch=EPOCH,
                    topology_revision=registry.topology_revision())
    assert cursor.position_for(slot_id) == (gen_epoch, 12)   # component freshly established at now


# ============================================================ shape validation

def test_wait_bad_owner_shape_is_invalid_request(wired):
    wait, board, ledger, registry, backend, sup = wired
    with pytest.raises(NelixError) as ei:
        wait.wait("has space", ORCH, None)
    assert ei.value.code == "invalid_request"


def test_wait_bad_orchestration_shape_is_invalid_request(wired):
    wait, board, ledger, registry, backend, sup = wired
    with pytest.raises(NelixError) as ei:
        wait.wait(OWNER, "not-an-orch", None)
    assert ei.value.code == "invalid_request"
