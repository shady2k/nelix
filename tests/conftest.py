import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

from daemon.config import ExecutorSpec  # noqa: E402  (after sys.path insert)

EXECUTOR = "demo"

# The owner most tests drive as. `owner_id` is required on every caller-facing manager/RPC call
# (daemon/owner.py), so a test that omits it is a TypeError, not a silent pass — which is the
# point of the parameter having no default. Tests that care about ISOLATION rather than merely
# satisfying the signature use their own two owners: see tests/test_owner_isolation.py.
OWNER = "test-owner"

# nelix-9a4.4: the store is MANDATORY for the SessionManager. Every test that constructs
# a SessionManager needs a Store + StartLedger sharing the same database root, and every
# session_id must be router-assigned (reserved through the ledger). These helpers and
# fixtures provide the shared setup so individual tests don't repeat it.
_OID = "o-" + "a" * 32
_GID = "g-" + "b" * 32
_GEPOCH = "g-" + "c" * 32


def own(session_id, owner_id=OWNER):
    """Write the durable owner record a real `manager.start()` would have written.

    For tests that put a Session into `mgr._sessions` BY HAND. Those bypass start(), so they
    bypass the owner write, and every owner-filtered read then hides the session — which is
    fail-closed working correctly, not an obstacle to route around. This restores the state a
    real start leaves behind; it does not weaken the gate. Returns the session_id so it can wrap
    an injection inline.
    """
    from daemon import owner as _owner
    import paths as _paths
    _owner.write(_paths.sessions_root() / session_id, owner_id)
    return session_id


def make_spec(**overrides):
    fields = dict(command="x", args=[], env={}, driver="claude", launcher="local")
    fields.update(overrides)
    return ExecutorSpec(**fields)


@pytest.fixture(autouse=True)
def isolate_nelix_home(tmp_path_factory, monkeypatch):
    """Point $NELIX_HOME at a per-test scratch root for EVERY test."""
    root = tmp_path_factory.mktemp("nelix-home")
    monkeypatch.setenv("NELIX_HOME", str(root))
    return root


@pytest.fixture
def store_and_ledger(tmp_path):
    """Create a Store + StartLedger sharing the same database root.

    Returns a (store, ledger) tuple. The store is mandatory for SessionManager
    (nelix-9a4.4); every test that builds a SessionManager must use this fixture
    or construct its own.
    """
    from nelix_store.store import Store
    from nelix_store.ledger import StartLedger
    root = tmp_path / "nelix-db"
    root.mkdir()
    store = Store(root)
    ledger = StartLedger(root)
    return store, ledger


def serve(manager, token="t"):
    """Start a real HTTP server on an EPHEMERAL tcp port; return (srv, base_url). Race-free: the OS
    assigns the port at bind time and we read it back, so parallel xdist workers never collide."""
    from daemon.rpc_server import make_server
    from daemon.transport import Transport
    srv = make_server(manager, Transport.tcp("127.0.0.1", 0, token))
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def real_router(monkeypatch):
    """Spies on every subprocess.Popen call (so a test can assert exactly how many router
    processes were spawned) and guarantees cleanup: SIGTERM/kill each spawned process and remove
    the leaf runtime dir, so a router `ensure` brings up never survives the test."""
    spawned = []
    real_popen = subprocess.Popen

    def _spy(*a, **kw):
        p = real_popen(*a, **kw)
        spawned.append(p)
        return p

    monkeypatch.setattr(subprocess, "Popen", _spy)
    try:
        yield spawned
    finally:
        for p in spawned:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        shutil.rmtree(paths.router_sock().parent, ignore_errors=True)


def reserve_start(ledger, owner_id=OWNER, idempotency_key=None):
    """Reserve a start + assign generation via the ledger, returning the session_id.

    Every test MUST use router-assigned session_ids — the daemon-minted-id fallback
    no longer exists (nelix-9a4.4).
    """
    import uuid
    key = idempotency_key or f"k-{uuid.uuid4().hex[:8]}"
    r = ledger.reserve(idempotency_key=key, owner_id=owner_id,
                       orchestration_id=_OID, request_fingerprint="fp")
    ledger.assign_generation(r.session_id, _GID, _GEPOCH)
    return r.session_id
