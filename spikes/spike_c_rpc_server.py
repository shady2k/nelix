"""Throwaway: tiny authenticated host RPC server on loopback. Token + port from env."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["NELIX_RPC_TOKEN"]
PORT = int(os.environ.get("NELIX_RPC_PORT", "8787"))
BIND = os.environ.get("NELIX_RPC_BIND", "0.0.0.0")  # container-reachable (host.docker.internal)


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("X-Nelix-Token") != TOKEN:
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
