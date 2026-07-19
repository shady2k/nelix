"""nelix-3rm slice 3c.1: the REQUIRED real-daemon integration test.

A REAL daemon is spun up as the generation — the suite's own daemon start machinery: a real
`daemon.rpc_server.make_server` HTTP server over a real AF_UNIX socket, fronting a real
`daemon.manager.SessionManager` (the same construction test_manager_session_id uses). A POST /start
is routed through the FULL router stack (securely-established socket -> peercred'd HTTP server ->
StartPath -> registry -> RpcClient forward), and we verify a worker actually starts UNDER THE
ROUTER-ASSIGNED session id and the ledger row commits — no fabricated frames, the daemon's real
start path runs and creates the session.

The supervisor DISCOVERY is stubbed to point the registry at this real daemon (discovery is tested
separately in test_supervisor*.py); the daemon itself, its HTTP layer, its manager start path, and
the ledger are all real."""
import hashlib
import os
import threading

import pytest

import paths
from daemon.events import EventQueue
from daemon.manager import SessionManager
from daemon.rpc_server import make_server
from daemon.transport import Transport
from nelix_store.ledger import StartLedger
from router import runtime_dir as rd
from router.registry import GenerationRegistry
from router.server import make_router_server
from router.start import StartPath
from rpc_client import RpcClient

from tests.conftest import EXECUTOR, OWNER, make_spec
from tests._router_fakes import Supervisor


class _RealDaemon:
    """A real daemon (make_server + real SessionManager) as the generation. The session leaf is the
    suite's lightweight real session (test_manager_session_id's factory) so the manager's REAL start
    path runs end-to-end without needing an external CLI binary; `created` records every session the
    manager actually built, keyed by the id the start was driven with."""

    def __init__(self, sock_path):
        from nelix_store.store import Store
        store = Store(paths.nelix_root())
        self.created = []
        daemon = self

        class _Leaf:
            def __init__(self, sid, executor):
                self.sid = sid
                self.executor = executor

            def start(self, task, cwd):
                daemon.created.append({"sid": self.sid, "task": task, "cwd": cwd})

            def snapshot(self):
                return {"session_id": self.sid, "executor": self.executor,
                        "control_state": "busy", "task_delivery": "pending", "pending": False}

            def stop(self):
                pass

        def factory(sid, executor, spec, events):
            return _Leaf(sid, executor)

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
    p = f"/tmp/nxd{h}.sock"
    yield p
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass


def test_router_starts_a_real_worker_under_the_assigned_id_and_commits(daemon_sock, tmp_path):
    daemon = _RealDaemon(daemon_sock)
    router = rd.establish()
    ledger = StartLedger(paths.nelix_root())
    registry = GenerationRegistry(supervisor=Supervisor(daemon.transport))  # real /health probe
    server = make_router_server(router.socket, router.sock_path,
                                StartPath(ledger, registry), registry, "r-" + "0" * 32)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = RpcClient(Transport.unix(router.sock_path), OWNER)
        st, body = client._call("POST", "/start",
                                {"executor": EXECUTOR, "task": "do the work", "cwd": str(tmp_path),
                                 "owner_id": OWNER, "idempotency_key": "real-1"})
        assert st == 200
        assert body["status"] == "started"
        sid = body["session_id"]

        # The REAL daemon manager created exactly one session, UNDER the router-assigned id.
        assert [c["sid"] for c in daemon.created] == [sid]
        assert daemon.created[0]["task"] == "do the work"

        # The ledger committed the reservation to the epoch the router picked.
        row = ledger.lookup("real-1", owner_id=OWNER)
        assert row.state == "started"
        assert row.generation_id == body["generation_id"]

        # The durable owner record the daemon wrote is under the ROUTER-assigned id (spec §3/§7).
        assert (paths.sessions_root() / sid / "owner.json").exists()

        # A retry with the same idempotency_key does NOT start a second worker.
        st2, body2 = client._call("POST", "/start",
                                  {"executor": EXECUTOR, "task": "do the work", "cwd": str(tmp_path),
                                   "owner_id": OWNER, "idempotency_key": "real-1"})
        assert st2 == 200
        assert body2["session_id"] == sid
        assert len(daemon.created) == 1                # still ONE worker
    finally:
        server.shutdown()
        router.close()
        daemon.close()
