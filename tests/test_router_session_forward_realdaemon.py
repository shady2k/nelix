"""nelix-3rm slice 3c.2: the REQUIRED real-daemon integration test for session-keyed forwarding.

A REAL daemon (real `daemon.rpc_server.make_server` + a real `daemon.manager.SessionManager`, PTY
faked the same way test_owner_isolation.py's FakeSession does — only the PTY is not part of THIS
invariant) sits behind the FULL router stack: securely-established socket -> peercred'd HTTP server
-> StartPath -> registry -> RpcClient forward for /start, and SessionForward for the session-keyed
routes this slice adds. A session is started THROUGH THE ROUTER, then `screen` and `respond` are
routed through the router to that REAL session and the response is verified to have actually
reached the real daemon (a visible side effect: the answer typed into the fake session) and to
relay back through the router unchanged.

The spec ownership test (§7) is proved here too, the expensive way: a DIFFERENT owner_id routed
through the router for the SAME session must be rejected by the REAL daemon's owner.owns_session —
relayed faithfully through the router, never accepted by a router-side gate and never a router-side
403 the router invented itself."""
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
    manager's real owner-gating + real routes (screen, respond) to run end-to-end, with a visible
    side effect (`answers`) that proves a forwarded write actually reached THIS session."""

    _cols = 80
    _rows = 24

    def __init__(self, sid, executor):
        self.sid = sid
        self.executor = executor
        self.task = self.cwd = None
        self.answers = []
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
        self.answers.append(answer)
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
    p = f"/tmp/nxs{h}.sock"
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
    server = make_router_server(router.socket, router.sock_path,
                                StartPath(ledger, registry), registry, "r-" + "0" * 32)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = RpcClient(Transport.unix(router.sock_path), OWNER)
    try:
        yield client, daemon, tmp_path
    finally:
        server.shutdown()
        router.close()
        daemon.close()


def _start_a_session(client, tmp_path, owner_id=OWNER, key="real-sf-1"):
    st, body = client._call("POST", "/start",
                            {"executor": EXECUTOR, "task": "do the work", "cwd": str(tmp_path),
                             "owner_id": owner_id, "idempotency_key": key})
    assert st == 200, body
    return body["session_id"]


def test_screen_reaches_the_real_session_through_the_router(router_over_real_daemon):
    client, daemon, tmp_path = router_over_real_daemon
    sid = _start_a_session(client, tmp_path)
    st, body = client._call("GET", f"/screen?session_id={sid}&owner_id={OWNER}")
    assert st == 200
    assert body["screen"] == f"SCREEN OF {sid} TASK=do the work"


def test_respond_reaches_the_real_session_through_the_router(router_over_real_daemon):
    client, daemon, tmp_path = router_over_real_daemon
    sid = _start_a_session(client, tmp_path)
    st, body = client._call("POST", "/respond",
                            {"session_id": sid, "answer": "go with plan A", "owner_id": OWNER})
    assert st == 200
    assert body["status"] == "resumed"
    # The REAL session recorded the answer -- a forwarded write actually reached it, not just a
    # 200 the router fabricated.
    assert daemon.created[sid].answers == ["go with plan A"]


def test_a_different_owner_is_rejected_by_the_real_daemon_through_the_router_on_screen(
        router_over_real_daemon):
    # The spec ownership test (§7), on a forwarded route: harness Y must not read harness X's
    # terminal even holding X's real session id, and the REJECTION must be the REAL daemon's own
    # (relayed through the router), not a router-invented one.
    client, daemon, tmp_path = router_over_real_daemon
    sid = _start_a_session(client, tmp_path, owner_id=OWNER)
    st, body = client._call("GET", f"/screen?session_id={sid}&owner_id={OTHER_OWNER}")
    assert st == 200
    assert body == {"error": "unknown session"}


def test_a_different_owner_is_rejected_by_the_real_daemon_through_the_router_on_respond(
        router_over_real_daemon):
    client, daemon, tmp_path = router_over_real_daemon
    sid = _start_a_session(client, tmp_path, owner_id=OWNER)
    st, body = client._call("POST", "/respond",
                            {"session_id": sid, "answer": "an answer from Y",
                             "owner_id": OTHER_OWNER})
    assert st == 404
    assert body["status"] == "unknown_session"
    # Nothing was typed into X's session by Y's rejected attempt.
    assert daemon.created[sid].answers == []


def test_a_different_owner_is_rejected_on_dialog_too(router_over_real_daemon):
    # dialog is the route that reads straight off disk (bypassing the manager entirely) -- proving
    # the ownership gate holds there too, forwarded through the router, closes the exact route the
    # spec calls out by name.
    client, daemon, tmp_path = router_over_real_daemon
    sid = _start_a_session(client, tmp_path, owner_id=OWNER)
    st, body = client._call("GET", f"/dialog?session_id={sid}&owner_id={OTHER_OWNER}")
    assert st == 404
    assert body["error"] == "unknown session"
