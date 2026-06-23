import dataclasses
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


def make_server(session, token, host="127.0.0.1", port=8765):
    class Handler(BaseHTTPRequestHandler):
        def _auth(self):
            if self.headers.get("X-Nelix-Token") != token:
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if not self._auth():
                return
            p = urlparse(self.path)
            if p.path == "/wait":
                after = int(parse_qs(p.query).get("after_seq", ["0"])[0])
                evt = session.wait_event(after_seq=after, timeout=25)
                self._send(200, {"event": dataclasses.asdict(evt) if evt else None})
            elif p.path == "/status":
                self._send(200, session.snapshot())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._auth():
                return
            p = urlparse(self.path)
            body = self._read_json()
            if p.path == "/start":
                session.start(body["task"])
                self._send(200, {"status": "started"})
            elif p.path == "/respond":
                ok = session.respond(body["event_id"], body["answer"])
                self._send(200 if ok else 409,
                           {"status": "resumed"} if ok else {"error": "unknown event_id"})
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, *a):
            pass

    return ThreadingHTTPServer((host, port), Handler)
