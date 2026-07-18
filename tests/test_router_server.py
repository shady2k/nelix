"""nelix-3rm slice 3c.1 Part D: the router HTTP server — a ThreadingHTTPServer on the securely-bound
router socket, unix peercred auth (peer_is_self), dispatch by routing.classify. THIS slice implements
POST /start (ACTIVE_GENERATION) end-to-end plus a router GET /health (liveness + active generation +
router_epoch). Session-keyed / fan-out / operator routes honestly 404 (a clear body) until 3c.2.

The server is exercised over its REAL securely-established AF_UNIX socket (runtime_dir.establish())
via a same-uid client — which is exactly the peercred allow path."""
import socket as _socket
import threading

import pytest

import paths
from nelix_store.ledger import StartLedger
from router import runtime_dir as rd
from router.registry import GenerationRegistry
from router.server import make_router_server
from router.start import StartPath
from rpc_client import RpcClient
from daemon.transport import Transport

from conftest import EXECUTOR, OWNER
from _router_fakes import Backend, Supervisor


class _Wired:
    def __init__(self, backend=None):
        self.backend = backend or Backend()
        self.handle = rd.establish()
        self.ledger = StartLedger(paths.nelix_root())
        self.registry = GenerationRegistry(supervisor=Supervisor(self.backend.transport),
                                           health_probe=lambda t: self.backend.build_id)
        self.start_path = StartPath(self.ledger, self.registry)
        self.epoch = "r-" + "0" * 32
        self.server = make_router_server(self.handle.socket, self.handle.sock_path,
                                         self.start_path, self.registry, self.epoch)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def client(self):
        return RpcClient(Transport.unix(self.handle.sock_path), OWNER)

    def close(self):
        self.server.shutdown()
        self.handle.close()
        self.backend.close()


@pytest.fixture
def wired():
    w = _Wired()
    yield w
    w.close()


def _raw_over_unix(sock_path, request_bytes, timeout=4):
    """Send a HAND-CRAFTED HTTP request over the router's unix socket and return (status, body_text).

    Used to exercise malformed Content-Length headers a normal client cannot produce. The request
    sends `Connection: close` so the server closes after responding (no keep-alive hang), and a
    socket timeout guards against a server that BLOCKS on an unbounded read instead of rejecting —
    a timeout here IS the finding-#4 bug (rfile.read(-1) waiting for EOF)."""
    c = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    c.settimeout(timeout)
    c.connect(sock_path)
    try:
        c.sendall(request_bytes)
        chunks = []
        while True:
            b = c.recv(4096)
            if not b:
                break
            chunks.append(b)
        data = b"".join(chunks)
    finally:
        c.close()
    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split(b" ")[1]) if head else 0
    return status, body.decode(errors="replace")


def test_negative_content_length_is_a_stable_400_without_an_unbounded_read(wired):
    # Finding #4: a negative Content-Length must be rejected with a stable 400 envelope BEFORE any
    # read — never reach rfile.read(-1), which blocks until EOF and bypasses the 4 MiB cap.
    req = (b"POST /start HTTP/1.0\r\nHost: localhost\r\n"
           b"Content-Length: -1\r\nConnection: close\r\n\r\n")
    status, body = _raw_over_unix(wired.handle.sock_path, req)
    assert status == 400
    assert '"code": "invalid_request"' in body or '"code":"invalid_request"' in body


def test_non_integer_content_length_is_a_stable_400(wired):
    # Finding #4: a non-integer Content-Length must be a stable 400, not an unhandled exception that
    # drops the connection with a stderr traceback.
    req = (b"POST /start HTTP/1.0\r\nHost: localhost\r\n"
           b"Content-Length: notanumber\r\nConnection: close\r\n\r\n")
    status, body = _raw_over_unix(wired.handle.sock_path, req)
    assert status == 400
    assert "invalid_request" in body


def test_over_large_content_length_is_a_clear_too_large_error(wired):
    # Finding #4: an over-large body returns a clear "too large" error (413), not a misleading
    # invalid_request:owner_id from a silently-dropped body. Rejected on the HEADER, before reading.
    req = (b"POST /start HTTP/1.0\r\nHost: localhost\r\n"
           b"Content-Length: 5000000\r\nConnection: close\r\n\r\n")
    status, body = _raw_over_unix(wired.handle.sock_path, req)
    assert status == 413
    assert "too large" in body
    assert "owner_id" not in body                       # NOT the misleading downstream error


def test_foreign_uid_401_is_a_stable_envelope(wired, monkeypatch):
    # Finding #5: the 401 must be the stable NelixError envelope (with `retryable`), not a hand-rolled
    # body. A foreign uid cannot be forged over AF_UNIX, so drive the refuse path directly.
    import router.server as rs
    monkeypatch.setattr(rs, "peer_is_self", lambda conn: False)
    st, body = wired.client()._call("GET", "/health")
    assert st == 401
    assert body["error"]["code"] == "owner_mismatch"
    assert body["error"]["retryable"] is False


def test_non_nelix_error_in_dispatch_becomes_a_500_internal_envelope(wired, monkeypatch):
    # Finding #6: a non-NelixError escaping a handler (e.g. from registry.active()) must become a
    # stable 500 INTERNAL_ERROR envelope, never a bare 500 / dropped connection.
    def _boom(*a, **k):
        raise RuntimeError("unexpected internal fault")
    monkeypatch.setattr(wired.registry, "active", _boom)
    st, body = wired.client()._call("POST", "/start",
                                    {"executor": EXECUTOR, "task": "go", "cwd": "/repo",
                                     "owner_id": OWNER, "idempotency_key": "k-boom"})
    assert st == 500
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["retryable"] is False


def test_router_health_reports_epoch_and_active_generation(wired):
    body = wired.client().health()
    assert body["status"] == "ok"
    assert body["router_epoch"] == wired.epoch
    # Before any /start the registry has observed nothing, so active_generation is null (and /health
    # must NOT spawn a daemon as a side effect of a liveness probe).
    assert body["active_generation"] is None


def test_start_routes_through_the_active_generation(wired):
    # POST /start over the router: reserve+assign+forward+commit; the backend receives the
    # router-minted session_id.
    _, body = wired.client()._call("POST", "/start",
                                   {"executor": EXECUTOR, "task": "go", "cwd": "/repo",
                                    "owner_id": OWNER, "idempotency_key": "k-1"})
    assert body["status"] == "started"
    sid = body["session_id"]
    assert wired.backend.starts[0]["session_id"] == sid
    # /health now shows the active generation the start committed to.
    h = wired.client().health()
    assert h["active_generation"]["epoch"] == body["generation_id"]


def test_session_keyed_route_404s_until_3c2(wired):
    st, body = wired.client()._call("POST", "/respond",
                                    {"session_id": "s-" + "a" * 32, "answer": "1",
                                     "owner_id": OWNER})
    assert st == 404
    assert body["error"]["class"] == "session-keyed"
    assert "3c" in body["error"]["message"]


def test_fan_out_route_404s_until_3c2(wired):
    st, body = wired.client()._call("GET", f"/status?owner_id={OWNER}")
    assert st == 404
    assert body["error"]["class"] == "fan-out"


def test_unknown_route_404s(wired):
    st, body = wired.client()._call("GET", "/nonesuch")
    assert st == 404
    assert "error" in body


def test_start_error_body_is_a_stable_envelope(wired):
    # A generation failure is mapped to a stable retryable envelope, never a bare 500.
    wired.backend.mode = "error"
    st, body = wired.client()._call("POST", "/start",
                                    {"executor": EXECUTOR, "task": "go", "cwd": "/repo",
                                     "owner_id": OWNER, "idempotency_key": "k-err"})
    assert st == 503
    assert body["error"]["code"] == "generation_unavailable"
    assert body["error"]["retryable"] is True


def test_peercred_allows_same_uid_and_would_refuse_a_foreign_uid(wired):
    # The same-uid client above already proves the ALLOW path end-to-end (every request in this
    # module authenticated via peer_is_self). A FOREIGN uid is refused by peer_is_self returning
    # False -> 401; there is no same-machine way to forge a different peer uid over AF_UNIX (the
    # kernel supplies it via SO_PEERCRED/LOCAL_PEERCRED), so this asserts the mechanism directly.
    from daemon.transport import peer_is_self
    import socket as _s
    a, b = _s.socketpair(_s.AF_UNIX, _s.SOCK_STREAM)
    try:
        assert peer_is_self(a) is True                 # both ends are this same uid
    finally:
        a.close(); b.close()
