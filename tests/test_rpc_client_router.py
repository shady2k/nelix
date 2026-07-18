"""nelix-3rm (router slice 3c.1): the router assigns a session id BEFORE forwarding /start, so
RpcClient.start must thread an optional `session_id` into the POST body (additive — omitted is
byte-identical to before), and the router needs a no-owner /health probe to read a generation's
build-id."""
import http.server
import socket
import threading

import pytest

from conftest import EXECUTOR, OWNER
from daemon.transport import Transport
from rpc_client import ForwardConnectError, ForwardResponseError, RpcClient


def test_client_start_includes_session_id_when_provided(monkeypatch):
    c = RpcClient(Transport.tcp("x", 80, "t"), OWNER)
    seen = {}
    monkeypatch.setattr(c, "_forward_call",
                        lambda m, p, body, **k: seen.update(body=body) or {})
    sid = "s-" + "a" * 32
    c.start(EXECUTOR, "go", "/repo", session_id=sid)
    assert seen["body"] == {"executor": EXECUTOR, "task": "go", "cwd": "/repo",
                            "owner_id": OWNER, "session_id": sid}


def test_client_start_omits_session_id_when_not_provided(monkeypatch):
    # Additive: an omitted session_id is the exact pre-feature body (mirrors model=None).
    c = RpcClient(Transport.tcp("x", 80, "t"), OWNER)
    seen = {}
    monkeypatch.setattr(c, "_forward_call",
                        lambda m, p, body, **k: seen.update(body=body) or {})
    c.start(EXECUTOR, "go", "/repo")
    assert "session_id" not in seen["body"]


def test_start_connect_failure_is_forward_connect_error():
    """Findings #2: a forward that cannot ESTABLISH the connection (nothing listening) raises
    ForwardConnectError — the request never left the router, so the daemon definitely created no
    worker. The phase (connect), not the exception type, is what makes it DEFINITE."""
    c = RpcClient(Transport.tcp("127.0.0.1", 9, "t"), OWNER)   # discard port: connection refused
    with pytest.raises(ForwardConnectError):
        c.start(EXECUTOR, "go", "/repo", session_id="s-" + "a" * 32, timeout=2)


def test_start_pre_connect_oserror_is_forward_connect_error(tmp_path):
    """Finding #2: a PRE-CONNECT OSError that is NOT connection-refused (here NotADirectoryError —
    the unix socket path's parent is a regular file) is STILL a connect-phase failure -> definite.
    Previously the broad OSError branch mis-classified this family as ambiguous and stranded it."""
    afile = tmp_path / "not-a-dir"
    afile.write_text("x")
    c = RpcClient(Transport.unix(str(afile / "gen.sock")), OWNER)   # connect -> NotADirectoryError
    with pytest.raises(ForwardConnectError):
        c.start(EXECUTOR, "go", "/repo", session_id="s-" + "a" * 32, timeout=2)


def test_start_dropped_connection_is_forward_response_error():
    """Finding #2: a backend that ACCEPTS and reads the request, then drops the connection without a
    valid HTTP reply, raises ForwardResponseError — the request WAS delivered (a worker may exist),
    so it is AMBIGUOUS, not a definite no-worker failure."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _accept_read_close():
        conn, _ = srv.accept()
        try:
            conn.recv(4096)               # read (some of) the request, then close with no HTTP reply
        finally:
            conn.close()

    t = threading.Thread(target=_accept_read_close, daemon=True)
    t.start()
    try:
        c = RpcClient(Transport.tcp("127.0.0.1", port, "t"), OWNER)
        with pytest.raises(ForwardResponseError):
            c.start(EXECUTOR, "go", "/repo", session_id="s-" + "a" * 32, timeout=5)
    finally:
        t.join(2)
        srv.close()


def test_start_malformed_json_reply_is_forward_response_error():
    """Finding #3: a backend replying 200 with a body that is NOT JSON raises ForwardResponseError —
    the daemon answered (so a worker may exist) but the reply is unusable. AMBIGUOUS, retry-safe."""
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            body = b"this is not json{"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = RpcClient(Transport.tcp("127.0.0.1", port, "t"), OWNER)
        with pytest.raises(ForwardResponseError):
            c.start(EXECUTOR, "go", "/repo", session_id="s-" + "a" * 32, timeout=5)
    finally:
        srv.shutdown()


def test_client_health_gets_health_route(monkeypatch):
    c = RpcClient(Transport.tcp("x", 80, "t"), OWNER)
    seen = {}
    monkeypatch.setattr(c, "_call",
                        lambda m, p, body=None, **k: seen.update(m=m, p=p)
                        or (200, {"status": "ok", "generation_id": None}))
    body = c.health()
    assert body == {"status": "ok", "generation_id": None}
    assert seen == {"m": "GET", "p": "/health"}
