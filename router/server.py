"""The router's threaded HTTP server + peercred auth + dispatch (spec §1).

A ThreadingHTTPServer served on the SECURELY-established AF_UNIX socket (runtime_dir.establish() —
this server never binds; it adopts the pre-bound, verified socket). AUTH is unix peercred via
`peer_is_self` (the per-uid socket + 0700 dir + 0600 node are the boundary; there is no token). A
FOREIGN uid is refused 401; an unknown or unreportable uid is allowed, because the filesystem perms
are the real local gate (peer_is_self's documented policy).

DISPATCH is by operation CLASS (routing.classify), never by an operation's meaning — the table is
what keeps the router stable (spec §1). 3c.1 implemented POST /start (ACTIVE_GENERATION) + GET
/health (router-local liveness). 3c.2 (this slice) adds:
  * The SESSION-KEYED owner routes — GET /status (session-scoped)/dialog/screen, POST
    /respond/stop/restart — forwarded via SessionForward with owner_id PASSED THROUGH unchanged
    (spec §7: the router never interprets ownership, only the generation does).
  * The owner-EXEMPT executor plane — POST /hook/<sid>, /message/<sid> — forwarded via
    SessionForward.forward_secret with the per-session secret HEADER + raw body passed through,
    never owner-gated.
  * The router-LOCAL operator routes — GET /capabilities, /generation_list — answered by
    OperatorRoutes from the registry / the one active generation, never fanned out.
3c.3a adds:
  * The fan-out BOARD read — GET /status with NO session_id — merged across every tracked
    generation (N=1 today) by BoardForward, with an attached opaque vector cursor
    (nelix_contracts.cursor). See router/board.py for the merge + cursor construction.
3c.3b (this slice) adds:
  * The ORCHESTRATION /wait — GET /wait with owner_id + orchestration_id + the opaque vector cursor
    — one waiter for an orchestration's N workers, long-polling the generation(s) via the cursor
    (WaitForward, router/wait.py). Decodes the cursor (CURSOR_EXPIRED/BOARD_CHANGED resync markers),
    derives the orchestration's sessions from the owner-scoped ledger, and forwards the daemon's
    MULTI-SESSION wait; explicit no-wake signals for an empty orchestration or an unownable set.
Every route is now implemented; _dispatch_unimplemented remains for any still-classified operation
that has no handler.
"""
import json
import socket
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from nelix_contracts import routing
from nelix_contracts.errors import INTERNAL_ERROR, INVALID_REQUEST, OWNER_MISMATCH, NelixError

from daemon.transport import peer_is_self
from router.board import BoardForward
from router.operator import OperatorRoutes
from router.restart import RestartPath
from router.session_forward import SessionForward
from router.start import http_status
from router.wait import WaitForward

_MAX_BODY = 4 * 1024 * 1024        # 4 MiB post-auth body cap (mirrors the daemon's rpc_server)

# Returned by _read_raw_body when it has ALREADY sent an error response (a bad Content-Length): the
# POST handler must stop, not respond again.
_STOP = object()

# URL path -> operation NAME. The router dispatches on the operation's CLASS (routing.classify), so
# this table only has to turn a route into the operation name the classifier understands. /hook/<sid>
# and /message/<sid> carry the id in the path (matched by prefix). /health is NOT here — it is a
# router-local liveness route, not a generation operation. Every one of these paths is now handled
# explicitly below (this table stays a complete route->operation map for whatever still falls
# through to `_dispatch_unimplemented`, exactly as "/start" already did in 3c.1 despite never
# reaching that fallback itself).
_POST_OPS = {"/start": "start", "/respond": "respond", "/stop": "stop", "/restart": "restart"}
_GET_OPS = {"/screen": "screen", "/dialog": "dialog", "/status": "status", "/wait": "wait",
           "/capabilities": "capabilities", "/generation_list": "generation_list"}


def _one(qs, key):
    """The single value of `key` in a parsed query dict, or None if absent. Present-but-empty
    (`key=`) is returned as `""`, not None — the session-keyed forward layer's own validators (not
    this dispatch layer) decide whether an empty value is acceptable."""
    vals = qs.get(key)
    return vals[0] if vals else None


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


def make_router_server(bound_socket, sock_path, start_path, registry, router_epoch,
                       store=None, archive_epoch=None):
    # Constructed here (not threaded through the signature) so every existing caller of
    # make_router_server keeps working unchanged — both take only `registry` (+ router_epoch),
    # already parameters of this function.
    session_forward = SessionForward(registry)
    restart_path = RestartPath(start_path.ledger, registry)
    operator_routes = OperatorRoutes(registry, router_epoch)
    # S2a.2: the router owns the archive board read. store is threaded from app.py so BoardForward
    # can call store.read_board_snapshot(owner_id) directly. archive_epoch is the per-process
    # epoch for the archive cursor component, minted like router_epoch.
    board_forward = BoardForward(registry, router_epoch, store=store, archive_epoch=archive_epoch)
    # The orchestration /wait waiter (3c.3b). Wired off the SAME shared StartLedger as the start
    # path (start_path.ledger) — one instance, never per-request (nelix-91y).
    wait_forward = WaitForward(start_path.ledger, registry, router_epoch,
                                store=store, archive_epoch=archive_epoch)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _auth(self):
            # Unix peercred: reject only a KNOWN foreign uid; unknown/unreportable is allowed because
            # the 0700 dir + 0600 socket are the real local boundary (peer_is_self's policy). The
            # refusal is the STABLE envelope (with `retryable`), not a hand-rolled body.
            if peer_is_self(self.connection):
                return True
            self._send(401, NelixError(OWNER_MISMATCH,
                                       "peer uid is not this router's uid").to_envelope())
            return False

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _safe_send(self, code, obj):
            """_send that tolerates a client that has already gone: the error path must not itself
            raise (which would drop the connection with a stderr traceback)."""
            try:
                self._send(code, obj)
            except OSError:
                pass

        def _guarded(self, dispatch):
            """Run a dispatch body, turning ANY escaping error into a stable envelope — a NelixError
            to its mapped status, anything else to a 500 INTERNAL_ERROR — so a raw exception (e.g. a
            non-NelixError escaping registry.active() / the start path) never reaches the client as a
            bare 500/stacktrace/dropped connection (finding #6)."""
            try:
                dispatch()
            except NelixError as e:
                self._safe_send(http_status(e.code), e.to_envelope())
            except Exception:
                self._safe_send(500, NelixError(INTERNAL_ERROR,
                                                "internal router error").to_envelope())

        def _read_raw_body(self):
            """Read the request body's Content-Length bytes; on a bad length, SEND a stable error and
            return _STOP. ALWAYS called on a POST before responding to a VALID body — even for an
            unimplemented route — so the socket is drained: a handler that answers without consuming
            the body leaves the client's in-flight sendall racing a server-side connection close.

            Content-Length is parsed DEFENSIVELY before any read (finding #4): a non-integer or
            negative length is rejected 400 (a negative one would otherwise reach rfile.read(-1) and
            block until EOF — unbounded, past the 4 MiB cap; a non-integer would raise past every
            guard into a bare 500), and a body over the cap is rejected 413 with a clear too-large
            message — never read into memory, never silently dropped into a misleading downstream
            'owner_id' error."""
            raw_len = self.headers.get("Content-Length", "0")
            try:
                n = int(raw_len)
            except (TypeError, ValueError):
                self._send(400, NelixError(INVALID_REQUEST,
                                           f"invalid Content-Length: {raw_len!r}").to_envelope())
                return _STOP
            if n < 0:
                self._send(400, NelixError(INVALID_REQUEST,
                                           "invalid Content-Length: must not be negative").to_envelope())
                return _STOP
            if n > _MAX_BODY:
                self._send(413, NelixError(INVALID_REQUEST,
                                           f"request body too large: {n} bytes exceeds the "
                                           f"{_MAX_BODY}-byte limit").to_envelope())
                return _STOP
            try:
                return self.rfile.read(n)
            except OSError:
                return b""                         # truncated read: treat as empty (handler 400s on shape)

        def do_POST(self):
            if not self._auth():
                return
            self._guarded(self._dispatch_post)

        def _dispatch_post(self):
            path = urlparse(self.path).path
            raw = self._read_raw_body()            # drain the body regardless of route (see above)
            if raw is _STOP:                       # a bad Content-Length already answered
                return
            if path == "/start":
                self._handle_start(raw)
                return
            if path == "/restart":
                self._handle_restart(raw)
                return
            if path.startswith("/hook/") or path.startswith("/message/"):
                self._handle_secret_forward(path, raw)
                return
            if path in ("/respond", "/stop"):
                self._handle_session_post(path, raw)
                return
            self._dispatch_unimplemented("POST", path)

        def do_GET(self):
            if not self._auth():
                return
            self._guarded(self._dispatch_get)

        def _dispatch_get(self):
            path = urlparse(self.path).path
            if path == "/health":
                self._handle_router_health()
                return
            if path == "/capabilities":
                self._handle_capabilities()
                return
            if path == "/generation_list":
                self._handle_generation_list()
                return
            if path in ("/screen", "/dialog"):
                self._handle_session_get(path)
                return
            if path == "/status":
                qs = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                if _one(qs, "session_id"):
                    self._handle_session_get(path)
                    return
                # No session_id: the fan-out BOARD read (3c.3a).
                self._handle_board(qs)
                return
            if path == "/wait":
                self._handle_wait()
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

        def _handle_restart(self, raw):
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                err = NelixError(INVALID_REQUEST, "restart body must be a JSON object")
                self._send(http_status(err.code), err.to_envelope())
                return
            status, resp = restart_path.handle(body)
            self._send(status, resp)

        def _handle_session_get(self, path):
            # SESSION-KEYED owner routes (GET half): session_id/owner_id/every other field is a raw
            # PASSTHROUGH straight from the query string -- this dispatch layer does not type-check
            # offset/limit/raw/force/include_progress; the generation already validates those (the
            # router forwarding a bad one and relaying the generation's own 400 is exactly the
            # "relay faithfully, never reinterpret" contract session_forward documents).
            qs = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            owner_id = _one(qs, "owner_id")
            session_id = _one(qs, "session_id")
            if path == "/status":
                status, resp = session_forward.status(
                    owner_id, session_id, include_progress=_one(qs, "include_progress"))
            elif path == "/dialog":
                status, resp = session_forward.dialog(
                    owner_id, session_id, offset=_one(qs, "offset"), limit=_one(qs, "limit"))
            else:                                          # /screen
                status, resp = session_forward.screen(
                    owner_id, session_id, raw=_one(qs, "raw"), force=_one(qs, "force"))
            self._send(status, resp)

        def _handle_board(self, qs):
            # The fan-out BOARD read (3c.3a): GET /status with no session_id. owner_id is a raw
            # passthrough from the query string -- BoardForward validates its shape (same as every
            # owner-scoped route) and answers 200 with the merged board + vector cursor even when a
            # generation is unavailable (board_incomplete, never a hard error; see router/board.py).
            status, resp = board_forward.status(_one(qs, "owner_id"))
            self._send(status, resp)

        def _handle_session_post(self, path, raw):
            # SESSION-KEYED owner routes (POST half): respond/stop/restart. owner_id is the
            # CALLER's own, validated for SHAPE only and then forwarded UNCHANGED (spec §7) — the
            # generation is the only party that decides ownership; a NelixError from
            # session_forward (bad shape, or a forward-machinery failure) propagates to _guarded,
            # which maps it to its stable envelope. A successful forward's (status, body) is sent
            # back exactly as the generation answered it, including its own ownership verdict.
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                raise NelixError(INVALID_REQUEST, f"{path} body must be a JSON object")
            owner_id = body.get("owner_id")
            session_id = body.get("session_id")
            if path == "/respond":
                if "answer" not in body:
                    # A truly-absent "answer" must be its own clean 400, not a fabricated JSON
                    # null forwarded in its place — the daemon's own `body["answer"]` would accept
                    # a present-but-null value (only a MISSING key raises), which would silently
                    # mask the caller's mistake behind whatever the daemon does with a null answer.
                    raise NelixError(INVALID_REQUEST, "missing field: 'answer'")
                status, resp = session_forward.respond(
                    owner_id, session_id, body["answer"], decision_id=body.get("decision_id"))
            elif path == "/stop":
                status, resp = session_forward.stop(owner_id, session_id)
            else:                                          # /restart
                status, resp = session_forward.restart(owner_id, session_id,
                                                        force=body.get("force"))
            self._send(status, resp)

        def _handle_secret_forward(self, path, raw):
            # The owner-EXEMPT executor plane (spec §7): /hook/<sid>, /message/<sid>. NO owner_id
            # anywhere -- authenticated purely by the per-session secret HEADER, passed through
            # unchanged, alongside the RAW body (never parsed/re-serialized by the router).
            headers = {"X-Nelix-Hook-Secret": self.headers.get("X-Nelix-Hook-Secret", "")}
            content_type = self.headers.get("Content-Type")
            if content_type:
                headers["Content-Type"] = content_type
            status, resp = session_forward.forward_secret("POST", path, headers, raw)
            self._send(status, resp)

        def _handle_wait(self):
            # The ORCHESTRATION /wait (3c.3b): owner_id + orchestration_id + the opaque vector
            # cursor, all raw passthrough from the query string -- WaitForward validates the
            # owner_id/orchestration_id shapes and decodes the cursor itself, exactly as the board
            # read validates its own owner_id. A NelixError (bad shape, unavailable generation)
            # propagates to _guarded, which maps it to its stable envelope.
            qs = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            status, resp = wait_forward.wait(
                _one(qs, "owner_id"), _one(qs, "orchestration_id"), _one(qs, "cursor"))
            self._send(status, resp)

        def _handle_capabilities(self):
            status, resp = operator_routes.capabilities()
            self._send(status, resp)

        def _handle_generation_list(self):
            status, resp = operator_routes.generation_list()
            self._send(status, resp)

        def _handle_router_health(self):
            # Report the CURRENTLY-observed active generation (registry.generations()), never
            # active(): a liveness probe must not spawn a daemon as a side effect.
            gens = registry.generations()
            active = None
            if gens:
                g = gens[0]
                active = {"generation_id": g.generation_id,
                          "generation_epoch": g.epoch,
                          "build_id": g.build_id,
                          "transport": getattr(g.transport, "kind", None)}
            self._send(200, {"status": "ok", "router_epoch": router_epoch,
                             "active_generation": active})

        def _dispatch_unimplemented(self, method, path):
            # Every ad-hoc 404 below is still the STABLE envelope ({error:{code,message,retryable}})
            # — a client relying on that contract must not misclassify an unimplemented route.
            # INVALID_REQUEST is the closest existing code: none of the others fit "operation not
            # (yet) supported by this router" (UNSUPPORTED_BY_GENERATION is spec §8's CROSS-
            # GENERATION incompatibility — a genuinely different case daemon/manager.py's
            # _session_capabilities already documents NOT fabricating for an unrelated meaning; the
            # same reasoning applies here). The 404 HTTP status and human message (naming that the
            # board/wait arrive in 3c.3) are unchanged — only the body gains a real code+retryable.
            op = _operation_for(method, path)
            if op is None:
                err = NelixError(INVALID_REQUEST, f"no such route: {method} {path}")
                self._send(404, err.to_envelope())
                return
            try:
                cls = routing.classify(op)
            except routing.UnknownOperation:
                err = NelixError(INVALID_REQUEST, f"unknown operation: {op}")
                self._send(404, err.to_envelope())
                return
            err = NelixError(
                INVALID_REQUEST,
                f"operation {op!r} (class {cls}) is not implemented in this router")
            self._send(404, err.to_envelope())

    return _PreboundUnixHTTPServer(bound_socket, sock_path, Handler)
