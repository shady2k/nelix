"""nelix-3rm (router slice 3c.1): the router assigns a session id BEFORE forwarding /start, so
RpcClient.start must thread an optional `session_id` into the POST body (additive — omitted is
byte-identical to before), and the router needs a no-owner /health probe to read a generation's
build-id."""
import http.server
import socket
import threading

import pytest

from tests.conftest import EXECUTOR, OWNER
from daemon.transport import Transport
from rpc_client import ForwardConnectError, ForwardResponseError, RpcClient, raw_forward


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


# ============================================================ forward_raw / raw_forward
#
# nelix-3rm slice 3c.2: the router's session-keyed forward (respond/stop/restart/screen/dialog/
# status) must relay the GENERATION's exact HTTP status, not just its body (start's phase-split
# _forward_call discards status because /start decides success from the body alone — see its
# docstring). forward_raw is the same phase-split machinery, additionally returning (status, body).
# raw_forward is the owner-EXEMPT sibling for /hook and /message: no RpcClient (no owner to
# construct one with), a transport + arbitrary headers (the per-session secret) + a raw body.

def test_forward_raw_relays_the_generations_exact_status_and_body():
    # A non-200 (here 403, mirroring an owner-mismatch a generation might one day return) must
    # come back UNCHANGED — forward_raw must not reinterpret it as a failure.
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            body = b'{"error": "unknown session"}'
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = RpcClient(Transport.tcp("127.0.0.1", port, "t"), OWNER)
        status, body = c.forward_raw("POST", "/respond", {"session_id": "s-x", "answer": "1"})
        assert status == 403
        assert body == {"error": "unknown session"}
    finally:
        srv.shutdown()


def test_forward_raw_connect_failure_is_forward_connect_error():
    c = RpcClient(Transport.tcp("127.0.0.1", 9, "t"), OWNER)   # discard port: connection refused
    with pytest.raises(ForwardConnectError):
        c.forward_raw("GET", "/screen", None, timeout=2)


def test_forward_raw_dropped_connection_is_forward_response_error():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _accept_read_close():
        conn, _ = srv.accept()
        try:
            conn.recv(4096)
        finally:
            conn.close()

    t = threading.Thread(target=_accept_read_close, daemon=True)
    t.start()
    try:
        c = RpcClient(Transport.tcp("127.0.0.1", port, "t"), OWNER)
        with pytest.raises(ForwardResponseError):
            c.forward_raw("GET", "/screen", None, timeout=5)
    finally:
        t.join(2)
        srv.close()


def test_forward_call_still_returns_only_the_body(monkeypatch):
    # _forward_call (used by start()) must keep its existing (body-only) contract even though it
    # now delegates to forward_raw internally -- this is what makes the refactor non-breaking.
    c = RpcClient(Transport.tcp("x", 80, "t"), OWNER)
    monkeypatch.setattr(c, "forward_raw", lambda m, p, body, **k: (200, {"ok": True}))
    assert c._forward_call("POST", "/start", {}) == {"ok": True}


def test_raw_forward_passes_headers_and_body_through_unchanged_and_relays_status():
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            seen["body"] = self.rfile.read(n)
            seen["secret"] = self.headers.get("X-Nelix-Hook-Secret")
            seen["token"] = self.headers.get("X-Nelix-Token")
            body = b'{"status": "queued", "id": "q_1"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, body = raw_forward(Transport.tcp("127.0.0.1", port, "tok-1"), "POST",
                                   "/message/s-" + "a" * 32,
                                   headers={"X-Nelix-Hook-Secret": "sek-1"},
                                   body=b'{"kind": "note", "text": "hi"}')
        assert status == 200
        assert body == {"status": "queued", "id": "q_1"}
        assert seen["body"] == b'{"kind": "note", "text": "hi"}'
        assert seen["secret"] == "sek-1"
        assert seen["token"] == "tok-1"           # tcp transport still carries its own auth token
    finally:
        srv.shutdown()


def test_raw_forward_connect_failure_is_forward_connect_error():
    with pytest.raises(ForwardConnectError):
        raw_forward(Transport.tcp("127.0.0.1", 9, "t"), "POST", "/hook/s-" + "a" * 32,
                   body=b"{}", timeout=2)


def test_raw_forward_over_unix_omits_the_tcp_token(tmp_path):
    # unix transport: no X-Nelix-Token (peercred is the boundary), mirroring _prep's own rule.
    # A SHORT path outside tmp_path: AF_UNIX sun_path is capped (~104 bytes on macOS), and
    # pytest's tmp_path is routinely longer than that (mirrors test_router_start_realdaemon.py's
    # daemon_sock fixture, which hits the same limit).
    import hashlib
    import os
    import socket as _socket

    seen = {}
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:8]
    sock_path = f"/tmp/nxr{h}.sock"

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            seen["token"] = self.headers.get("X-Nelix-Token")
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

    class UnixServer(http.server.HTTPServer):
        address_family = _socket.AF_UNIX

    srv = UnixServer(sock_path, H, bind_and_activate=False)
    srv.server_bind = lambda: None
    srv.socket = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    srv.socket.bind(sock_path)
    srv.socket.listen(5)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, _ = raw_forward(Transport.unix(sock_path), "POST", "/hook/s-" + "a" * 32,
                                headers={"X-Nelix-Hook-Secret": "sek"}, body=b"{}")
        assert status == 204
        assert "token" not in seen or seen["token"] is None
    finally:
        srv.shutdown()
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
