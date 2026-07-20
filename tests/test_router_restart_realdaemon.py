"""nelix-3rm slice 3c.4: the REQUIRED real-daemon acceptance tests for ROUTER RESTART/RECONCILE.

Mirrors test_router_board_realdaemon.py / test_router_session_forward_realdaemon.py /
test_router_wait_realdaemon.py's harness: a REAL daemon (real `daemon.rpc_server.make_server` + a
real `daemon.manager.SessionManager`, PTY faked) sits behind the FULL router stack. This slice PROVES
the spec's router-restart requirement end-to-end (Implementation-plans §3, Testing §; spec
docs/superpowers/specs/2026-07-16-nelix-standalone-runtime-design.md, §1/§4): "a router restart
mid-session KILLS NOTHING; restart reconciles from generation snapshots."

A "router restart" is simulated the way the spec frames it (a control-plane process replaceable
without touching the data plane): tear down the FIRST router (release its socket + flock, mirroring
router/app.py's SIGTERM -> finally teardown — test_router_app.py already proves that exact release
mechanically; this file does not re-prove SIGTERM plumbing, only the RESTART CYCLE's effect on the
router's own state: socket+lock release/reacquire, a fresh GenerationRegistry, a fresh router_epoch),
then bring up a SECOND router (fresh `rd.establish()`, fresh `GenerationRegistry`, fresh router_epoch,
fresh `StartLedger` instance) against the SAME real daemon — never torn down, never touched by either
router. The daemon is the "generation"; the router holds no PTYs and streams nothing, so its replacement
is a moment of connection refusal + client retry, never a killed session (module docstring,
router/app.py).

Test A (kills nothing + non-spawning discovery on EVERY forward path): starts a real session through
the OLD router, restarts, and proves the FRESH router (a) still finds the daemon+session alive, (b)
serves the board (/status), (c) serves a SESSION-KEYED forward (/screen), and (d) serves the
ORCHESTRATION /wait — all via the SAME non-spawning mechanism (registry.active()'s
_ensure_available()+held_generation(), see router/registry.py), and (e) never spawned a second daemon
(a supervisor stand-in that counts ensure_running() calls stays at 0 throughout).

Test B (cursor expiry, spec §4): a board cursor minted by the OLD router (old router_epoch) is
presented to the NEW router's /wait -- nelix_contracts.cursor.decode's router_epoch mismatch check
must fire CURSOR_EXPIRED, surfaced as the wait's explicit resync marker (never silently accepted). The
NEW router's own freshly-minted cursor is then proven to work normally (a real event wakes a real
wait through it).

Test C (the exclusive lock across the restart cycle, extends test_router_app.py's SIGTERM/reacquire
lock test to the FULL cycle against a live generation): while the OLD router holds the per-NELIX_HOME
flock, a second `establish()` fails closed (RouterLockHeld) -- no two routers ever bind the same
socket. Once the OLD router releases it, the NEW router re-acquires the SAME lock and actually SERVES
-- both a router-local route (/health) and a forward that reaches the surviving generation's session.

Reconcile note (N=1) + the Plan-4 seam: for N=1 (today), "which generation serves session X" needs no
lookup at all -- there is exactly one tracked slot, and the registry's non-spawning discovery
(held_generation()) finds it fresh after every router restart, so "the one discovered generation"
IS every session's generation. Tests A/B/C all exercise this trivial N=1 reconcile implicitly: the
pre-restart session is served by the post-restart registry with zero bespoke reconcile code. What
Plan 4 needs when N>1 (session_id -> generation_id, resolved from the StartLedger's `starts` row) is
already commented as a structural seam at its call sites -- router/session_forward.py's module
docstring ("ROUTING (structural seam for Plan 4)") and its `_forward` method, and router/wait.py's
module docstring (part 3) and its `gen_sessions = sessions` line. This slice does not touch that seam
(no N>1 reconcile is built here) -- it only proves the N=1 case the seam already collapses to.
"""
import hashlib
import os
import threading

import pytest

import paths
from daemon.events import EventQueue
from daemon.launchers.base import ExecutorCapabilities
from daemon.manager import SessionManager
from daemon.rpc_server import make_server
from daemon.session import RespondOutcome
from daemon.transport import Transport
from nelix_store.ledger import StartLedger
from router import runtime_dir as rd
from router.registry import GenerationRegistry
from router.runtime_dir import RouterLockHeld
from router.server import make_router_server
from router.start import StartPath
from rpc_client import RpcClient

from tests.conftest import EXECUTOR, OWNER, make_spec
from tests._router_fakes import Supervisor


class _StubDriver:
    hook_capable = True


class _StubLauncher:
    capabilities = ExecutorCapabilities(isolation_class="host", can_attach=False)


class _FakeSession:
    """A session with no PTY (mirrors test_owner_isolation.py's FakeSession, and every other
    _realdaemon test's copy of it): real enough for the manager's real owner-gating, real board-wide
    status(), real screen/respond routes, and real event ring to all run end-to-end."""

    _cols = 80
    _rows = 24

    def __init__(self, sid, executor):
        self.sid = sid
        self.executor = executor
        self.task = self.cwd = None
        self._driver = _StubDriver()
        self._launcher = _StubLauncher()

    def start(self, task, cwd):
        self.task, self.cwd = task, cwd

    def snapshot(self):
        return {"session_id": self.sid, "executor": self.executor,
                "control_state": "busy", "task_delivery": "pending", "pending": False}

    def is_working(self):
        return False

    def screen(self, raw=False):
        return f"SCREEN OF {self.sid} TASK={self.task}"

    def respond(self, answer, decision_id=None):
        return RespondOutcome("resumed", seq=1, decision_id="dec-1")

    def has_pending_async(self, decision_id):
        return False

    def stop(self):
        pass
    def observe(self): pass
    def last_observed(self): return 0.0
    def orphan_marked_ts(self): return None
    def mark_orphaned(self, grace): pass


class _RealDaemon:
    """The ONE generation this whole file exercises. Constructed ONCE per test and NEVER torn down
    or touched by either router -- the daemon and its manager/event-ring persist across the
    simulated router restart exactly as the spec says a real daemon does."""

    def __init__(self, sock_path):
        from nelix_store.store import Store
        store = Store(paths.nelix_root())
        self.created = {}
        daemon = self

        def factory(sid, executor, spec, events):
            s = _FakeSession(sid, executor)
            daemon.created[sid] = s
            return s

        self.events = EventQueue()
        self.manager = SessionManager({EXECUTOR: make_spec()}, self.events, store,
                                      session_factory=factory, concurrency_limit=5)
        self.server = make_server(self.manager, Transport.unix(sock_path))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.transport = Transport.unix(sock_path)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def daemon_sock(tmp_path):
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:8]
    p = f"/tmp/nxr{h}.sock"
    yield p
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass


@pytest.fixture
def daemon(daemon_sock):
    d = _RealDaemon(daemon_sock)
    try:
        yield d
    finally:
        d.close()


class _CountingSupervisor(Supervisor):
    """The SAME fixed-transport supervisor stand-in every _realdaemon test uses, plus a counter on
    the SPAWNING call -- so a test can assert a fresh registry's non-spawning discovery never took
    it, the same property test_router_registry.py's `_BoomIfSpawned` proves at the unit level, here
    proven against a REAL daemon reached through the FULL router stack."""

    def __init__(self, transport):
        super().__init__(transport)
        self.ensure_calls = 0

    def ensure_running(self):
        self.ensure_calls += 1
        return super().ensure_running()


class _Router:
    def __init__(self, handle, ledger, registry, epoch, server, client):
        self.handle = handle
        self.ledger = ledger
        self.registry = registry
        self.epoch = epoch
        self.server = server
        self.client = client

    def close(self):
        self.server.shutdown()
        self.handle.close()
        self.ledger.close()


def _spin_up_router(epoch, supervisor) -> _Router:
    """Bring up ONE router: establish() the secure runtime dir (an `nelix router` process's first
    act), build a FRESH StartLedger instance (its backing SQLite file is durable under NELIX_HOME --
    a NEW instance still reads every row a PRIOR router process ever wrote; this is the whole
    mechanism the N=1 reconcile note above relies on) + a FRESH GenerationRegistry, and serve.
    Mirrors router/app.py::main()'s wiring order exactly, minus the process boundary."""
    handle = rd.establish()
    ledger = StartLedger(paths.nelix_root())
    registry = GenerationRegistry(supervisor=supervisor)
    start_path = StartPath(ledger, registry)
    server = make_router_server(handle.socket, handle.sock_path, start_path, registry, epoch)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = RpcClient(Transport.unix(handle.sock_path), OWNER)
    return _Router(handle, ledger, registry, epoch, server, client)


def _start(client, tmp_path, owner_id, key, orchestration_id=None):
    body = {"executor": EXECUTOR, "task": "do the work", "cwd": str(tmp_path),
            "owner_id": owner_id, "idempotency_key": key}
    if orchestration_id is not None:
        body["orchestration_id"] = orchestration_id
    st, body = client._call("POST", "/start", body)
    assert st == 200, body
    return body["session_id"]


# ================================================================= A: kills nothing + discovers

def test_router_restart_kills_nothing_and_the_fresh_router_discovers_the_surviving_daemon(
        daemon, tmp_path):
    orch = "o-" + "a" * 32
    old = _spin_up_router("r-" + "1" * 32, Supervisor(daemon.transport))
    sid = _start(old.client, tmp_path, OWNER, "restart-a", orch)
    assert daemon.created[sid].task == "do the work"        # the real session is really running

    # SIMULATE A ROUTER RESTART: tear down the OLD router (releases its socket + flock, exactly what
    # router/app.py's SIGTERM handler's `finally` does -- test_router_app.py already proves that
    # release mechanically works over a real SIGTERM; this asserts what it's FOR). The daemon and
    # its manager/event-ring are NEVER touched.
    old.close()
    assert daemon.created[sid].task == "do the work"        # still alive: the router never held a PTY

    # A FRESH router: new runtime-dir establish() (re-acquiring the now-released lock), a BRAND NEW
    # GenerationRegistry (nothing observed yet -- `_active is None`), a fresh router_epoch, a fresh
    # StartLedger instance. `new_sup` counts the SPAWNING call so "did not spawn a second daemon" is
    # an assertion, not an assumption.
    new_sup = _CountingSupervisor(daemon.transport)
    new = _spin_up_router("r-" + "2" * 32, new_sup)
    try:
        # Sanity: the fresh registry really has observed NOTHING yet -- the successful forwards
        # below are real discovery, not an artifact of some pre-warmed cache.
        assert new.registry.generations() == []

        # (b) the BOARD discovers the survivor (non-spawning discover=True, router/board.py).
        st, board = new.client._call("GET", f"/status?owner_id={OWNER}")
        assert st == 200
        assert sid in board["sessions"]
        assert board["board_incomplete"] is False

        # (c) a SESSION-KEYED forward reaches it (registry.active(), router/session_forward.py).
        st, body = new.client._call("GET", f"/screen?session_id={sid}&owner_id={OWNER}")
        assert st == 200
        assert body["screen"] == f"SCREEN OF {sid} TASK=do the work"

        # (d) the ORCHESTRATION /wait reaches it too (registry.active(), router/wait.py). A cursor
        # from BEFORE a REAL event's publish + the event already on the ring by call time -> the
        # daemon's wait_event_any wakes immediately (no real 25s long-poll needed for this assertion,
        # same pattern test_router_wait_realdaemon.py uses).
        _, board2 = new.client._call("GET", f"/status?owner_id={OWNER}")
        cursor = board2["cursor"]
        evt = daemon.events.publish(sid, EXECUTOR, "waiting_for_user", "answer me",
                                    "waiting_for_user")
        st, wb = new.client._call(
            "GET", f"/wait?owner_id={OWNER}&orchestration_id={orch}&cursor={cursor}")
        assert st == 200, wb
        assert wb["event"]["session_id"] == sid
        assert wb["event"]["seq"] == evt.seq

        # (e) none of the above ever spawned a SECOND daemon -- the fresh registry's first-ever
        # observation came entirely from the non-spawning discovery/identity read.
        assert new_sup.ensure_calls == 0
    finally:
        new.close()


# ================================================================================ B: cursor expiry

def test_pre_restart_cursor_expires_and_the_new_routers_own_cursor_works_normally(daemon, tmp_path):
    orch = "o-" + "b" * 32
    old = _spin_up_router("r-" + "3" * 32, Supervisor(daemon.transport))
    sid = _start(old.client, tmp_path, OWNER, "restart-b", orch)
    _, old_board = old.client._call("GET", f"/status?owner_id={OWNER}")
    old_cursor = old_board["cursor"]                          # minted under the OLD router_epoch
    old.close()

    new = _spin_up_router("r-" + "4" * 32, Supervisor(daemon.transport))
    try:
        # spec §4: a router restart changes router_epoch -> the OLD cursor decodes to
        # CURSOR_EXPIRED against the NEW router, surfaced as /wait's explicit resync marker (never
        # silently accepted, never a hard error the caller must special-case).
        st, wb = new.client._call(
            "GET", f"/wait?owner_id={OWNER}&orchestration_id={orch}&cursor={old_cursor}")
        assert st == 200
        assert wb == {"event": None, "cursor_expired": True}

        # The caller resyncs via /status and re-arms: the NEW router's OWN freshly-minted cursor
        # works NORMALLY -- a real event wakes a real wait through it.
        _, new_board = new.client._call("GET", f"/status?owner_id={OWNER}")
        new_cursor = new_board["cursor"]
        evt = daemon.events.publish(sid, EXECUTOR, "waiting_for_user", "a fresh decision",
                                    "waiting_for_user")
        st, wb2 = new.client._call(
            "GET", f"/wait?owner_id={OWNER}&orchestration_id={orch}&cursor={new_cursor}")
        assert st == 200, wb2
        assert wb2["event"]["session_id"] == sid
        assert wb2["event"]["seq"] == evt.seq
    finally:
        new.close()


# =========================================================== C: the exclusive lock across restart

def test_the_lock_is_exclusive_then_reacquired_and_served_across_a_restart_cycle(daemon, tmp_path):
    old = _spin_up_router("r-" + "5" * 32, Supervisor(daemon.transport))
    sid = _start(old.client, tmp_path, OWNER, "restart-c")
    try:
        # No two routers: a second establish() while OLD holds the per-NELIX_HOME flock fails
        # closed (extends test_router_app.py's SIGTERM/reacquire lock test with a live generation
        # and a live session behind the held router, rather than an idle one).
        with pytest.raises(RouterLockHeld):
            rd.establish()
    finally:
        old.close()                                           # releases the flock (+ the socket)

    # The NEW router RE-ACQUIRES the SAME lock (no RouterLockHeld this time)...
    new = _spin_up_router("r-" + "6" * 32, Supervisor(daemon.transport))
    try:
        # ...and actually SERVES: a router-local route (/health, never forwarded)...
        st, health = new.client._call("GET", f"/health?owner_id={OWNER}")
        assert st == 200
        assert health["router_epoch"] == "r-" + "6" * 32

        # ...and a forward that reaches the surviving generation's pre-restart session.
        st, body = new.client._call("GET", f"/screen?session_id={sid}&owner_id={OWNER}")
        assert st == 200
        assert body["screen"] == f"SCREEN OF {sid} TASK=do the work"
    finally:
        new.close()
