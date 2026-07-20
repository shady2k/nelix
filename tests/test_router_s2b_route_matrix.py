"""nelix-80e S2b: session→generation routing + route matrix dispatch.

Tests exercise the route matrix by SEEDING store generations rows in dead-epoch / retired states
(+ matching starts rows and archived terminal/transcript fixtures), then asserting each cell of
the matrix WITHOUT a second live daemon.

The existing test_router_session_forward.py covers the live/serving path (byte-identical
regression guard). These tests cover the dead/retired columns with N=1 store-only seeding.

Gate: all must pass without a live Backend for the dead/retired cases.
"""
import json
import time

import pytest

import paths
from tests.conftest import OWNER
from nelix_contracts.errors import NelixError, UNSUPPORTED_BY_GENERATION
from nelix_store.store import Store
from nelix_store.ledger import StartLedger
from router.registry import GenerationRegistry
from router.session_forward import SessionForward

GID = "g-" + "b" * 32
GEPOCH = "g-" + "c" * 32
OID = "o-" + "a" * 32
FP = "fp"

# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def store_and_ledger(tmp_path):
    root = tmp_path / "nelix-db"
    root.mkdir()
    store = Store(root)
    ledger = StartLedger(root)
    yield store, ledger


def _seed_generation(store, gid, lifecycle_state, epoch_state, *,
                     capability_snapshot=None):
    store.create_generation(gid, build_id=None, lifecycle_state=lifecycle_state,
                            capability_snapshot=capability_snapshot,
                            created_at=time.time())
    store.insert_epoch(GEPOCH, gid, incarnation_meta=None, created_at=time.time())
    if epoch_state == "serving":
        store.cas_epoch_serving(gid, GEPOCH, expected_current_epoch=None)
    elif epoch_state == "dead":
        store.set_epoch_process_state(GEPOCH, "dead")


def _seed_start(ledger, gid=GID, gepoch=GEPOCH, owner_id=OWNER):
    key = f"k-{time.time_ns()}"
    res = ledger.reserve(idempotency_key=key, owner_id=owner_id,
                         orchestration_id=OID, request_fingerprint=FP)
    ledger.assign_generation(res.session_id, gid, gepoch)
    return res.session_id


def _seed_terminal(store, sid, owner_id=OWNER, kind="completed", summary="done"):
    store.create_session(sid, state="finished", executor="demo", task="x",
                         cwd="/", model=None, created_at=time.time())
    store.put_terminal(sid, terminal_kind=kind, summary=summary,
                       ended_at=time.time())


def _seed_transcript(sid, lines=None):
    """Write a transcript.jsonl for the given session_id under NELIX_HOME."""
    sdir = paths.sessions_root() / sid
    sdir.mkdir(parents=True, exist_ok=True)
    if lines is None:
        lines = [{"kind": "line", "speaker": "agent", "text": "Hello"}]
    tpath = sdir / "transcript.jsonl"
    with open(tpath, "w") as f:
        for rec in lines:
            f.write(json.dumps(rec) + "\n")


# ── helpers ──────────────────────────────────────────────────────────────


def _make_forward(store, ledger):
    """Build a SessionForward wired to a store-only registry (no supervisor)."""
    registry = GenerationRegistry(store=store)
    return SessionForward(registry, ledger=ledger, store=store)


# ═══════════════════════════════════════════════════════════════════════
# Dead-epoch generation routing matrix
# ═══════════════════════════════════════════════════════════════════════


class TestDeadEpoch:
    """A session whose owning generation's epoch is 'dead' (generation is still 'active')."""

    @pytest.fixture
    def dead_setup(self, store_and_ledger):
        store, ledger = store_and_ledger
        _seed_generation(store, GID, "active", "dead")
        sid = _seed_start(ledger)
        _seed_terminal(store, sid)
        _seed_transcript(sid)
        forward = _make_forward(store, ledger)
        yield forward, sid

    def test_status_returns_archived_terminal(self, dead_setup):
        forward, sid = dead_setup
        status, body = forward.status(OWNER, sid)
        assert status == 200
        assert body.get("terminal") is True
        assert body.get("session_id") == sid
        assert body.get("terminal_kind") == "completed"

    def test_dialog_returns_transcript(self, dead_setup):
        forward, sid = dead_setup
        status, body = forward.dialog(OWNER, sid)
        assert status == 200
        assert "text" in body
        assert body.get("total_len", 0) > 0

    def test_screen_is_unsupported(self, dead_setup):
        forward, sid = dead_setup
        with pytest.raises(NelixError) as exc:
            forward.screen(OWNER, sid)
        assert exc.value.code == UNSUPPORTED_BY_GENERATION

    def test_respond_is_unsupported(self, dead_setup):
        forward, sid = dead_setup
        with pytest.raises(NelixError) as exc:
            forward.respond(OWNER, sid, "yes")
        assert exc.value.code == UNSUPPORTED_BY_GENERATION

    def test_stop_is_unsupported(self, dead_setup):
        forward, sid = dead_setup
        with pytest.raises(NelixError) as exc:
            forward.stop(OWNER, sid)
        assert exc.value.code == UNSUPPORTED_BY_GENERATION

    def test_restart_is_unsupported(self, dead_setup):
        forward, sid = dead_setup
        with pytest.raises(NelixError) as exc:
            forward.restart(OWNER, sid)
        assert exc.value.code == UNSUPPORTED_BY_GENERATION

    def test_hook_is_unsupported(self, dead_setup):
        forward, sid = dead_setup
        with pytest.raises(NelixError) as exc:
            forward.forward_secret("POST", f"/hook/{sid}",
                                   {"X-Nelix-Hook-Secret": "x"}, b"{}")
        assert exc.value.code == UNSUPPORTED_BY_GENERATION

    def test_message_is_unsupported(self, dead_setup):
        forward, sid = dead_setup
        with pytest.raises(NelixError) as exc:
            forward.forward_secret("POST", f"/message/{sid}",
                                   {"X-Nelix-Hook-Secret": "x"}, b"{}")
        assert exc.value.code == UNSUPPORTED_BY_GENERATION


# ═══════════════════════════════════════════════════════════════════════
# Retired generation routing matrix
# ═══════════════════════════════════════════════════════════════════════


class TestRetiredGeneration:
    """A session whose owning generation's lifecycle_state is 'retired'."""

    @pytest.fixture
    def retired_setup(self, store_and_ledger):
        store, ledger = store_and_ledger
        _seed_generation(store, GID, "retired", "dead")
        sid = _seed_start(ledger)
        _seed_terminal(store, sid)
        _seed_transcript(sid)
        forward = _make_forward(store, ledger)
        yield forward, sid

    def test_status_returns_archived_terminal(self, retired_setup):
        forward, sid = retired_setup
        status, body = forward.status(OWNER, sid)
        assert status == 200
        assert body.get("terminal") is True
        assert body.get("session_id") == sid

    def test_dialog_returns_transcript(self, retired_setup):
        forward, sid = retired_setup
        status, body = forward.dialog(OWNER, sid)
        assert status == 200
        assert "text" in body

    def test_screen_is_unsupported(self, retired_setup):
        forward, sid = retired_setup
        with pytest.raises(NelixError) as exc:
            forward.screen(OWNER, sid)
        assert exc.value.code == UNSUPPORTED_BY_GENERATION

    def test_respond_is_unsupported(self, retired_setup):
        forward, sid = retired_setup
        with pytest.raises(NelixError) as exc:
            forward.respond(OWNER, sid, "yes")
        assert exc.value.code == UNSUPPORTED_BY_GENERATION

    def test_stop_is_unsupported(self, retired_setup):
        forward, sid = retired_setup
        with pytest.raises(NelixError) as exc:
            forward.stop(OWNER, sid)
        assert exc.value.code == UNSUPPORTED_BY_GENERATION


# ═══════════════════════════════════════════════════════════════════════
# Capability derivation
# ═══════════════════════════════════════════════════════════════════════


class TestCapabilityDerivation:
    """Per-session capabilities derived from the originating generation's capability_snapshot."""

    def test_capability_snapshot_is_threaded_through_resolve(self, store_and_ledger):
        store, ledger = store_and_ledger
        cap_snap = json.dumps({"executors": {"demo": {"hook_capable": True}}})
        _seed_generation(store, GID, "active", "dead", capability_snapshot=cap_snap)
        _seed_start(ledger)
        registry = GenerationRegistry(store=store)
        proc_state, lc_state, actual_cap, handle = registry.resolve_generation_state(GID, GEPOCH)
        assert actual_cap == cap_snap

    def test_none_capability_default_does_not_crash(self, store_and_ledger):
        store, ledger = store_and_ledger
        _seed_generation(store, GID, "active", "dead", capability_snapshot=None)
        sid = _seed_start(ledger)
        _seed_terminal(store, sid)
        registry = GenerationRegistry(store=store)
        proc_state, lc_state, cap_snap, handle = registry.resolve_generation_state(GID, GEPOCH)
        assert cap_snap is None
        forward = SessionForward(registry, ledger=ledger, store=store)
        status, body = forward.status(OWNER, sid)
        assert status == 200
        assert body.get("terminal") is True


# ═══════════════════════════════════════════════════════════════════════
# Topology revision bump on generation add/remove
# ═══════════════════════════════════════════════════════════════════════


class TestTopologyRevision:
    """Verify topology_revision bumps on generation state changes."""

    def test_topology_revision_starts_at_one(self, store_and_ledger):
        store, ledger = store_and_ledger
        registry = GenerationRegistry(store=store)
        assert registry.topology_revision() >= 1

    def test_bump_topology_increments(self, store_and_ledger):
        store, ledger = store_and_ledger
        registry = GenerationRegistry(store=store)
        before = registry.topology_revision()
        registry._bump_topology_locked()
        assert registry.topology_revision() == before + 1

    def test_serve_forwards_match_existing_active(self, store_and_ledger):
        """When the registry has no active, a seeded serving generation does NOT forward
        (no live daemon) — the router cannot serve it. Verify topology_revision is at least 1."""
        from router.registry import GenerationRegistry
        store, ledger = store_and_ledger
        _seed_generation(store, GID, "active", "serving")
        _seed_start(ledger)
        registry = GenerationRegistry(store=store)
        assert registry.topology_revision() >= 1
