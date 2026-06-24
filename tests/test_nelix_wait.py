import json, signal, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from conftest import EXECUTOR

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
        {"event": {"seq": 5, "session_id": "s1", "event_id": "evt-x", "executor": EXECUTOR,
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
    # the waiter must NOT echo the raw TUI `summary` (last-8-grid-lines = box-drawing
    # chrome) — that floods the notify_on_complete output; the agent reads details via
    # nelix_status instead.
    assert "summary" not in rec


def test_nelix_wait_reads_token_from_token_file(tmp_path):
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["tok"] = self.headers.get("X-Nelix-Token")
            body = json.dumps({"event": {"seq": 7, "session_id": "s2", "event_id": "evt-y",
                                         "executor": EXECUTOR, "kind": "done",
                                         "summary": "done"}}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *a): pass

    srv = ThreadingHTTPServer(("127.0.0.1", 8792), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tf = tmp_path / ".active.json"
    tf.write_text(json.dumps({"pid": 1, "port": 8792, "token": "filetok"}))
    try:
        out = subprocess.check_output(
            [sys.executable, str(ROOT / "bin" / "nelix-wait"),
             "--base", "http://127.0.0.1:8792", "--after", "0", "--token-file", str(tf)],
            env={"PATH": "/usr/bin:/bin"}, timeout=10, text=True)  # NO NELIX_RPC_TOKEN in env
    finally:
        srv.shutdown()
    assert seen["tok"] == "filetok"   # token read from the file and sent in the header
    assert json.loads(out.strip())["event_id"] == "evt-y"


def test_nelix_wait_exits_cleanly_when_daemon_unreachable(tmp_path):
    # If the daemon is gone/unreachable, the waiter must NOT crash with a traceback
    # (exit 1) — it should exit 0 with {"kind":"none"} so Hermes wakes and reconciles
    # via nelix_status instead of seeing a scary background-process failure.
    tf = tmp_path / ".active.json"
    tf.write_text(json.dumps({"pid": 1, "port": 1, "token": "t"}))
    out = subprocess.check_output(
        [sys.executable, str(ROOT / "bin" / "nelix-wait"),
         "--base", "http://127.0.0.1:1", "--after", "0", "--token-file", str(tf)],
        env={"PATH": "/usr/bin:/bin"}, timeout=10, text=True)
    assert json.loads(out.strip()) == {"kind": "none"}


def test_nelix_wait_graceful_interrupt():
    hit = threading.Event()
    class BlockingHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            hit.set()
            body = json.dumps({"event": None}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
            import time; time.sleep(30)
        def log_message(self, *a): pass
    srv = ThreadingHTTPServer(("127.0.0.1", 8791), BlockingHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bin" / "nelix-wait"),
             "--base", "http://127.0.0.1:8791", "--after", "0"],
            env={"NELIX_RPC_TOKEN": "t", "PATH": "/usr/bin:/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert hit.wait(timeout=5), "Server never received request"
        proc.send_signal(signal.SIGINT)
        exit_code = proc.wait(timeout=5)
        assert exit_code == 0, f"Expected exit 0, got {exit_code}"
        out = proc.stdout.read()
        rec = json.loads(out.strip())
        assert rec == {"kind": "none"}, f"Expected {{'kind': 'none'}}, got {rec}"
    finally:
        srv.shutdown()
        if proc is not None:
            proc.kill()
