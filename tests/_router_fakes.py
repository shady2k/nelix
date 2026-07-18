"""Shared in-process fakes for router tests: a generation-backend HTTP stub (daemon GET /health +
POST /start) and a supervisor stand-in pointing at it. NOT a test module itself (leading underscore
keeps pytest from collecting it)."""
import http.server
import json
import threading
from urllib.parse import parse_qs, urlparse

from daemon.transport import Transport


class Backend:
    """In-process HTTP stub of a generation: GET /health + POST /start (accepts a router-assigned
    session_id), PLUS (nelix-3rm 3c.2) the session-keyed owner routes (status/dialog/screen/
    respond/stop/restart), the owner-EXEMPT executor plane (hook/message), and GET /capabilities —
    just enough of the daemon's real wire shapes (daemon/rpc_server.py) to prove the ROUTER forwards
    each faithfully. mode="ok" echoes success bodies; mode="error" makes /start return 409.

    Ownership is simulated by `owns`: a session_id is "owned" by whatever owner_id started it
    (recorded automatically on /start; settable directly via `owns[sid] = owner_id` too). A
    request whose owner_id does not match gets exactly the shape the REAL daemon returns for that
    route (mirrors daemon/rpc_server.py: /status and /screen answer 200 with an error BODY; /dialog,
    /respond, /stop, /restart answer 404) — so a router test asserting "the wrong-owner rejection
    relays through" is checking the SAME shape the real daemon produces, not a fake invented one.
    """

    def __init__(self, *, mode="ok", build_id="b-1", hook_secret="hook-secret-1"):
        self.mode = mode
        self.build_id = build_id
        self.hook_secret = hook_secret
        self.starts = []
        self.owns = {}                 # session_id -> owner_id that "started" it
        self.calls = []                 # every non-start/health request: {"method","path","headers","body"}
        backend = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_empty(self, code):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _qs(self):
                return parse_qs(urlparse(self.path).query, keep_blank_values=True)

            def _owned(self, sid, owner_id):
                return sid in backend.owns and backend.owns[sid] == owner_id

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/health":
                    self._send(200, {"status": "ok", "rpc_protocol": 1,
                                     "generation_id": backend.build_id})
                    return
                qs = self._qs()
                backend.calls.append({"method": "GET", "path": self.path, "query": qs})
                one = lambda k: qs.get(k, [None])[0]
                owner_id = one("owner_id")
                sid = one("session_id")
                if path == "/status":
                    if not self._owned(sid, owner_id):
                        self._send(200, {"error": "unknown session"}); return
                    self._send(200, {"session_id": sid, "control_state": "idle",
                                     "include_progress": one("include_progress"), "cursor": 0})
                elif path == "/dialog":
                    if not sid:
                        self._send(400, {"error": "missing session_id"}); return
                    if not self._owned(sid, owner_id):
                        self._send(404, {"error": "unknown session"}); return
                    self._send(200, {"chunk": "DIALOG " + sid, "offset": one("offset") or "0",
                                     "next_offset": 7, "total_len": 7, "at_end": True})
                elif path == "/screen":
                    if not self._owned(sid, owner_id):
                        self._send(200, {"error": "unknown session"}); return
                    self._send(200, {"screen": "SCREEN " + sid, "cols": 80, "rows": 24})
                elif path == "/capabilities":
                    self._send(200, {"executors": {"demo": {"hook_capable": True}},
                                     "rpc_protocol": 1})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                path = urlparse(self.path).path
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n)
                if path.startswith("/hook/") or path.startswith("/message/"):
                    backend.calls.append({"method": "POST", "path": path,
                                          "headers": dict(self.headers), "raw_body": raw})
                    provided = self.headers.get("X-Nelix-Hook-Secret", "")
                    if provided != backend.hook_secret:
                        self._send(401, {"error": "unauthorized"}); return
                    if path.startswith("/hook/"):
                        self._send_empty(204)
                    else:
                        self._send(200, {"status": "queued", "id": "q_1"})
                    return
                body = json.loads(raw or b"{}")
                if path == "/start":
                    backend.starts.append(body)
                    if backend.mode == "error":
                        self._send(409, {"error": "generation is full"}); return
                    sid = body.get("session_id")
                    backend.owns[sid] = body.get("owner_id")
                    self._send(200, {"operation": "start", "status": "started", "session_id": sid,
                                     "snapshot": {"session_id": sid, "control_state": "busy"},
                                     "next_after_seq": 0, "next_action": "end_turn"})
                    return
                backend.calls.append({"method": "POST", "path": path, "body": body})
                sid = body.get("session_id")
                owner_id = body.get("owner_id")
                if path == "/respond":
                    if not self._owned(sid, owner_id):
                        self._send(404, {"operation": "respond", "status": "unknown_session",
                                         "session_id": sid}); return
                    self._send(200, {"operation": "respond", "status": "resumed",
                                     "session_id": sid, "answer": body.get("answer")})
                elif path == "/stop":
                    if not self._owned(sid, owner_id):
                        self._send(404, {"operation": "stop", "status": "unknown_session",
                                         "session_id": sid}); return
                    self._send(200, {"operation": "stop", "status": "stopped", "session_id": sid})
                elif path == "/restart":
                    if not self._owned(sid, owner_id):
                        self._send(404, {"operation": "restart", "status": "unknown_session"}); return
                    self._send(200, {"operation": "restart", "status": "restarted",
                                     "session_id": sid, "force": body.get("force", False)})
                else:
                    self._send(404, {"error": "not found"})

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    @property
    def transport(self):
        return Transport.tcp("127.0.0.1", self.port, "t")

    def close(self):
        self._srv.shutdown()


class Supervisor:
    """Supervisor stand-in whose active_generation()/held_generation() return a fixed
    (transport, incarnation) pair — exactly as the registry consumes it (the full health-checked read
    outside the lock, the authoritative lock-holder read under it)."""

    def __init__(self, transport, inc=None):
        self._t = transport
        self.inc = inc or {"pid": 1, "start_fingerprint": "fp"}

    def active_generation(self):
        return (self._t, self.inc)

    def held_generation(self):
        return (self._t, self.inc)

    def ensure_running(self):
        return self._t
