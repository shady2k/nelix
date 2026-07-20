"""nelix-3rm slice 3c.3b: the REQUIRED real-daemon integration test for the ORCHESTRATION /wait.

Mirrors test_router_board_realdaemon.py's harness: a REAL daemon (real `daemon.rpc_server.make_server`
+ a real `daemon.manager.SessionManager`, PTY faked) sits behind the FULL router stack. This proves
the whole chain the fakes cannot:
  * the daemon's NEW MULTI-SESSION wait primitive (`EventQueue.wait_event_any`) wakes on a REAL event
    published to a REAL session's REAL event ring -- not a fabricated frame;
  * the router derives the orchestration's sessions from its OWN owner-scoped ledger, forwards the
    daemon's multi-session /wait, and advances ONLY the generation's cursor component;
  * owner isolation: owner Y waiting on owner Z's orchestration_id sees NONE of Z's sessions (the
    ledger filters on owner_id), against real ownership records on real disk, not a fake's `owns`.
"""
import hashlib
import os
import threading

import pytest

import paths
from tests.conftest import EXECUTOR, OWNER, make_spec
from daemon.events import EventQueue
from daemon.launchers.base import ExecutorCapabilities
from daemon.manager import SessionManager
from daemon.rpc_server import make_server
from daemon.transport import Transport
from nelix_contracts.cursor import decode
from nelix_store.ledger import StartLedger
from router import runtime_dir as rd
from router.registry import GenerationRegistry
from router.server import make_router_server
from router.start import StartPath
from rpc_client import RpcClient

from tests._router_fakes import Supervisor

OTHER_OWNER = "harness-y"


class _StubDriver:
    hook_capable = True


class _StubLauncher:
    capabilities = ExecutorCapabilities(isolation_class="host", can_attach=False)


class _FakeSession:
    """A session with no PTY (mirrors test_router_board_realdaemon.py): real enough for the manager's
    real owner-gating + real event ring to run end-to-end."""

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

    def stop(self):
        pass

    def observe(self): pass
    def last_observed(self): return 0.0
    def orphan_marked_ts(self): return None
    def mark_orphaned(self, grace): pass


class _RealDaemon:
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
    p = f"/tmp/nxw{h}.sock"
    yield p
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass


@pytest.fixture
def router_over_real_daemon(daemon_sock, tmp_path):
    daemon = _RealDaemon(daemon_sock)
    router = rd.establish()
    ledger = StartLedger(paths.nelix_root())
    registry = GenerationRegistry(supervisor=Supervisor(daemon.transport))
    epoch = "r-" + "0" * 32
    server = make_router_server(router.socket, router.sock_path,
                                StartPath(ledger, registry), registry, epoch)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = RpcClient(Transport.unix(router.sock_path), OWNER)
    try:
        yield client, daemon, tmp_path, registry, epoch, router.sock_path
    finally:
        server.shutdown()
        router.close()
        daemon.close()


def _start(client, tmp_path, owner_id, key, orch):
    st, body = client._call("POST", "/start",
                            {"executor": EXECUTOR, "task": "do the work", "cwd": str(tmp_path),
                             "owner_id": owner_id, "idempotency_key": key,
                             "orchestration_id": orch})
    assert st == 200, body
    return body["session_id"]


def test_orchestration_wait_wakes_on_a_real_event_across_the_multi_session_primitive(
        router_over_real_daemon):
    # TWO sessions in one orchestration -> the router forwards the daemon's MULTI-SESSION /wait, so
    # this exercises EventQueue.wait_event_any against a REAL event ring end-to-end. A real event on
    # the SECOND session wakes the wait and returns the advanced cursor + the real event.
    client, daemon, tmp_path, registry, epoch, _sock = router_over_real_daemon
    orch = "o-" + "a" * 32
    s1 = _start(client, tmp_path, OWNER, "k-wait-1", orch)
    s2 = _start(client, tmp_path, OWNER, "k-wait-2", orch)

    # Read the board cursor BEFORE publishing, so the cursor's seq sits behind the new event.
    _, board = client._call("GET", f"/status?owner_id={OWNER}")
    cursor = board["cursor"]

    # A REAL event on s2's REAL ring (not a fabricated frame).
    evt = daemon.events.publish(s2, EXECUTOR, "waiting_for_user", "answer me", "waiting_for_user")

    st, wb = client._call(
        "GET", f"/wait?owner_id={OWNER}&orchestration_id={orch}&cursor={cursor}")
    assert st == 200, wb
    assert wb["event"]["session_id"] == s2                 # the member that published woke the set
    assert wb["event"]["seq"] == evt.seq
    # ONLY this generation's component advanced, to the real event's seq.
    new_cursor = decode(wb["cursor"], router_epoch=epoch,
                        topology_revision=registry.topology_revision())
    slot_id = registry.generations()[0].generation_id
    assert new_cursor.position_for(slot_id)[1] == evt.seq
    # sanity: s1 exists and is a distinct session in the same orchestration (the set had two).
    assert s1 != s2


def test_orchestration_wait_is_owner_isolated_against_the_real_ledger(router_over_real_daemon):
    # Owner Z runs an orchestration; owner Y waits on the SAME orchestration_id. The router's
    # owner-scoped ledger returns NONE of Z's sessions to Y -> Y gets the explicit empty-orchestration
    # no-wake signal, never Z's session (and the daemon is never even asked to wait on it).
    client, daemon, tmp_path, registry, epoch, sock_path = router_over_real_daemon
    orch = "o-" + "b" * 32
    z_client = RpcClient(Transport.unix(sock_path), OTHER_OWNER)
    z_sid = _start(z_client, tmp_path, OTHER_OWNER, "k-z", orch)
    # A real event on Z's session exists on the ring...
    daemon.events.publish(z_sid, EXECUTOR, "waiting_for_user", "z's decision", "waiting_for_user")

    # ...but Y waiting on the same orchestration id sees an EMPTY orchestration, never Z's event.
    st, wb = client._call("GET", f"/wait?owner_id={OWNER}&orchestration_id={orch}")
    assert st == 200
    assert wb["event"] is None and wb["empty_orchestration"] is True


def test_orchestration_wait_daemon_gates_a_session_the_ledger_but_not_owner_json_grants(
        router_over_real_daemon):
    # The daemon (not the router) is the ownership AUTHORITY. If a session is in owner OWNER's ledger
    # but owner.json (on real disk) says otherwise, the daemon owner-gates it away -> the router
    # surfaces the unownable no-wake signal. Forge the divergence: reserve a session for OWNER, but
    # write its durable owner.json as OTHER_OWNER (the router would never diverge these; the point is
    # that even if they diverged, the daemon's owner.json gate is what actually holds).
    from conftest import own
    client, daemon, tmp_path, registry, epoch, sock_path = router_over_real_daemon
    forged_orch = "o-" + "d" * 32
    ledger = StartLedger(paths.nelix_root())
    try:
        r = ledger.reserve(idempotency_key="k-forged", owner_id=OWNER,
                           orchestration_id=forged_orch, request_fingerprint="fp")
        own(r.session_id, OTHER_OWNER)                     # owner.json = OTHER_OWNER, not OWNER
        _, board = client._call("GET", f"/status?owner_id={OWNER}")
        st, wb = client._call(
            "GET", f"/wait?owner_id={OWNER}&orchestration_id={forged_orch}&cursor={board['cursor']}")
        assert st == 200
        # The daemon owner-gated the session away (owner.json != OWNER) -> the set reduced to empty.
        assert wb["event"] is None and wb["unownable"] is True
    finally:
        ledger.close()
