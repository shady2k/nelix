"""The router's threaded HTTP server + peercred auth + dispatch (spec §1).

A ThreadingHTTPServer served on the SECURELY-established AF_UNIX socket (runtime_dir.establish() —
this server never binds; it adopts the pre-bound, verified socket). AUTH is unix peercred via
`peer_is_self` (the per-uid socket + 0700 dir + 0600 node are the boundary; there is no token). A
FOREIGN uid is refused 401; an unknown or unreportable uid is allowed, because the filesystem perms
are the real local gate (peer_is_self's documented policy).

DISPATCH is by operation CLASS (routing.classify), never by an operation's meaning — the table is
what keeps the router stable (spec §1). THIS slice implements only:
  * POST /start  (ACTIVE_GENERATION) end-to-end via StartPath.
  * GET  /health (router-local liveness: router_epoch + the active generation, WITHOUT spawning one).
Everything else honestly 404s with a body naming its class and that it lands in 3c.2 — an
unimplemented-yet route is honest, not dead code.
"""
import json
import socket
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from nelix_contracts import routing
from nelix_contracts.errors import INVALID_REQUEST, NelixError

from daemon.transport import peer_is_self
from router.start import http_status

_MAX_BODY = 4 * 1024 * 1024        # 4 MiB post-auth body cap (mirrors the daemon's rpc_server)

# URL path -> operation NAME. The router dispatches on the operation's CLASS (routing.classify), so
# this table only has to turn a route into the operation name the classifier understands. /hook/<sid>
# and /message/<sid> carry the id in the path (matched by prefix). /health is NOT here — it is a
# router-local liveness route, not a generation operation.
_POST_OPS = {"/start": "start", "/respond": "respond", "/stop": "stop", "/restart": "restart"}
_GET_OPS = {"/screen": "screen", "/dialog": "dialog", "/status": "status", "/wait": "wait"}


def _operation_for(method, path):
    if method == "POST":
        if path.startswith("/hook/"):
            return "hook"
        if path.startswith("/message/"):
            return "message"
        return _POST_OPS.get(path)
    if method == "GET":
        return _GET_OPS.get(path)
    return None


class _PreboundUnixHTTPServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that ADOPTS a pre-bound AF_UNIX socket instead of binding its own. The
    router must bind fd-relative + O_NOFOLLOW (runtime_dir.establish()); letting HTTPServer.__init__
    bind by pathname would reopen the very TOCTOU the secure establishment closes."""
    address_family = socket.AF_UNIX
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, bound_socket, server_address, handler):
        # Skip TCPServer.__init__'s socket creation + bind entirely; wire only BaseServer state,
        # adopt the securely-bound socket, then listen.
        socketserver.BaseServer.__init__(self, server_address, handler)
        self.socket = bound_socket
        # server_bind (skipped) is what normally sets these; set them here so nothing that reads
        # them hits an AttributeError, and so no reverse-DNS is attempted on an AF_UNIX path (the
        # daemon's UnixHTTPServer sets them for the same reason). Meaningless for AF_UNIX.
        self.server_name = "localhost"
        self.server_port = 0
        self.server_activate()               # listen() on the adopted socket


def make_router_server(bound_socket, sock_path, start_path, registry, router_epoch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _auth(self):
            # Unix peercred: reject only a KNOWN foreign uid; unknown/unreportable is allowed because
            # the 0700 dir + 0600 socket are the real local boundary (peer_is_self's policy).
            if peer_is_self(self.connection):
                return True
            self._send(401, {"error": {"code": "owner_mismatch",
                                       "message": "peer uid is not this router's uid"}})
            return False

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_raw_body(self):
            """Read the request body's Content-Length bytes, or None if it exceeds the cap. ALWAYS
            called on a POST before responding — even for an unimplemented route — so the socket is
            drained: a handler that answers without consuming the body leaves the client's in-flight
            sendall racing a server-side connection close (a flaky BrokenPipe under load)."""
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n > _MAX_BODY:
                return None
            try:
                return self.rfile.read(n)
            except OSError:
                return None

        def do_POST(self):
            if not self._auth():
                return
            path = urlparse(self.path).path
            raw = self._read_raw_body()            # drain the body regardless of route (see above)
            if path == "/start":
                self._handle_start(raw)
                return
            self._dispatch_unimplemented("POST", path)

        def do_GET(self):
            if not self._auth():
                return
            path = urlparse(self.path).path
            if path == "/health":
                self._handle_router_health()
                return
            self._dispatch_unimplemented("GET", path)

        def _handle_start(self, raw):
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                err = NelixError(INVALID_REQUEST, "start body must be a JSON object")
                self._send(http_status(err.code), err.to_envelope())
                return
            status, resp = start_path.handle(body)
            self._send(status, resp)

        def _handle_router_health(self):
            # Report the CURRENTLY-observed active generation (registry.generations()), never
            # active(): a liveness probe must not spawn a daemon as a side effect.
            gens = registry.generations()
            active = None
            if gens:
                g = gens[0]
                active = {"epoch": g.epoch, "build_id": g.build_id,
                          "transport": getattr(g.transport, "kind", None)}
            self._send(200, {"status": "ok", "router_epoch": router_epoch,
                             "active_generation": active})

        def _dispatch_unimplemented(self, method, path):
            op = _operation_for(method, path)
            if op is None:
                self._send(404, {"error": {"message": f"no such route: {method} {path}"}})
                return
            try:
                cls = routing.classify(op)
            except routing.UnknownOperation:
                self._send(404, {"error": {"message": f"unknown operation: {op}"}})
                return
            self._send(404, {"error": {
                "operation": op, "class": cls,
                "message": f"operation {op!r} (class {cls}) is not implemented in this router "
                           f"slice (3c.1); session-keyed/fan-out/operator routes arrive in 3c.2"}})

    return _PreboundUnixHTTPServer(bound_socket, sock_path, Handler)
