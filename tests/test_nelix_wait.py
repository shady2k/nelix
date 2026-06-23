import json, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _server(port, payloads):
    state = {"i": 0}
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payloads[min(state["i"], len(payloads) - 1)]).encode()
            state["i"] += 1
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a): pass
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_nelix_wait_reissues_then_prints_event():
    srv = _server(8790, [
        {"event": None},
        {"event": {"seq": 5, "session_id": "s1", "event_id": "evt-x", "executor": "claude_zai",
                   "kind": "waiting_for_user", "summary": "1. Yes / 3. No"}},
    ])
    try:
        out = subprocess.check_output(
            [sys.executable, str(ROOT / "bin" / "nelix-wait"),
             "--base", "http://127.0.0.1:8790", "--after", "0"],
            env={"NELIX_RPC_TOKEN": "t", "PATH": "/usr/bin:/bin"}, timeout=10, text=True)
    finally:
        srv.shutdown()
    rec = json.loads(out.strip())
    assert rec["event_id"] == "evt-x" and rec["session_id"] == "s1" and rec["seq"] == 5
