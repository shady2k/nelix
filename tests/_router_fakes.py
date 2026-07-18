"""Shared in-process fakes for router tests: a generation-backend HTTP stub (daemon GET /health +
POST /start) and a supervisor stand-in pointing at it. NOT a test module itself (leading underscore
keeps pytest from collecting it)."""
import http.server
import json
import threading

from daemon.transport import Transport


class Backend:
    """In-process HTTP stub of a generation: GET /health + POST /start (accepts a router-assigned
    session_id). mode="ok" echoes a start receipt; mode="error" returns a 409 error body."""

    def __init__(self, *, mode="ok", build_id="b-1"):
        self.mode = mode
        self.build_id = build_id
        self.starts = []
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

            def do_GET(self):
                if self.path == "/health":
                    self._send(200, {"status": "ok", "rpc_protocol": 1,
                                     "generation_id": backend.build_id})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path != "/start":
                    self._send(404, {"error": "not found"}); return
                backend.starts.append(body)
                if backend.mode == "error":
                    self._send(409, {"error": "generation is full"}); return
                sid = body.get("session_id")
                self._send(200, {"operation": "start", "status": "started", "session_id": sid,
                                 "snapshot": {"session_id": sid, "control_state": "busy"},
                                 "next_after_seq": 0, "next_action": "end_turn"})

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    @property
    def transport(self):
        return Transport.tcp("127.0.0.1", self.port, "t")

    def close(self):
        self._srv.shutdown()


class Supervisor:
    """Supervisor stand-in whose active_generation()/current_generation() return a fixed
    (transport, incarnation) pair — read together, exactly as the registry consumes it (the full
    read outside the lock, the cheap read under it)."""

    def __init__(self, transport, inc=None):
        self._t = transport
        self.inc = inc or {"pid": 1, "start_fingerprint": "fp"}

    def active_generation(self):
        return (self._t, self.inc)

    def current_generation(self):
        return (self._t, self.inc)

    def ensure_running(self):
        return self._t
