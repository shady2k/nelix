"""nelix-3rm slice 3c.3a: the REQUIRED real-daemon integration test for the fan-out BOARD read.

Mirrors test_router_session_forward_realdaemon.py's harness: a REAL daemon (real
`daemon.rpc_server.make_server` + a real `daemon.manager.SessionManager`, PTY faked the same way
test_owner_isolation.py's FakeSession does) sits behind the FULL router stack. Two harnesses start
a session each through the router's real /start, then GET /status with NO session_id (the
fan-out board) is routed through BoardForward -> the REAL daemon's owner-filtered board-wide
`status(session_id=None)` -- proving the OWNER FILTER survives the router's merge the expensive
way, against real ownership records on real disk, not a fake's simulated `owns` dict."""
import hashlib
import os
import threading

import pytest

import paths
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

from tests.conftest import EXECUTOR, OWNER, make_spec
from tests._router_fakes import Supervisor

OTHER_OWNER = "harness-y"


class _StubDriver:
    hook_capable = True


class _StubLauncher:
    capabilities = ExecutorCapabilities(isolation_class="host", can_attach=False)


class _FakeSession:
    """A session with no PTY (mirrors test_owner_isolation.py's FakeSession): real enough for the
    manager's real owner-gating + real board-wide status() to run end-to-end."""

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

        self.manager = SessionManager({EXECUTOR: make_spec()}, EventQueue(), store,
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
    p = f"/tmp/nxb{h}.sock"
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


def _start_a_session(client, tmp_path, owner_id, key):
    st, body = client._call("POST", "/start",
                            {"executor": EXECUTOR, "task": "do the work", "cwd": str(tmp_path),
                             "owner_id": owner_id, "idempotency_key": key})
    assert st == 200, body
    return body


def test_board_owner_filtering_survives_the_router_merge_against_the_real_daemon(
        router_over_real_daemon):
    # X and Y are TWO harnesses on the SAME real daemon (nelix-v96's class at the harness
    # boundary). X's board read must show only X's session -- the daemon's real owner.owns_session
    # gate, relayed faithfully through the router's merge, never a router-side re-filter and never
    # a router-side leak.
    client, daemon, tmp_path, registry, epoch, sock_path = router_over_real_daemon
    x_body = _start_a_session(client, tmp_path, OWNER, "board-real-x")
    other_client = RpcClient(Transport.unix(sock_path), OTHER_OWNER)
    y_body = _start_a_session(other_client, tmp_path, OTHER_OWNER, "board-real-y")

    st, body = client._call("GET", f"/status?owner_id={OWNER}")
    assert st == 200
    assert x_body["session_id"] in body["sessions"]
    assert y_body["session_id"] not in body["sessions"]
    assert body["board_incomplete"] is False


def test_board_cursor_round_trips_against_the_real_daemons_int_cursor(router_over_real_daemon):
    client, daemon, tmp_path, registry, epoch, _sock_path = router_over_real_daemon
    _start_a_session(client, tmp_path, OWNER, "board-real-cursor")
    st, body = client._call("GET", f"/status?owner_id={OWNER}")
    assert st == 200
    cursor = decode(body["cursor"], router_epoch=epoch,
                    topology_revision=registry.topology_revision())
    # fix-pass finding #1: the cursor's map KEY is the registry's STABLE slot_id, not the
    # per-incarnation epoch (which is now `/start`'s `generation_epoch`); the epoch is carried
    # as part of the VALUE.
    slot_id = registry.generations()[0].generation_id
    gen_epoch = registry.generations()[0].epoch
    real_cursor = daemon.manager.status(owner_id=OWNER)["cursor"]
    assert cursor.position_for(slot_id) == (gen_epoch, real_cursor)
