import json, signal, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_wait(env=None, args=(), timeout=10):
    """Run bin/nelix-wait and return CompletedProcess."""
    cmd = [sys.executable, str(ROOT / "bin" / "nelix-wait")] + list(args)
    return subprocess.run(cmd,
        env=env or {"PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=timeout)


def _start_fake_router(response_body):
    """Start a minimal HTTP server as fake router on a random TCP port.
    Returns (port, server)."""
    body_json = json.dumps(response_body).encode()

    class FakeRouter(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_json)))
            self.end_headers()
            self.wfile.write(body_json)

        def do_POST(self):
            return self.do_GET()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return port, srv


# ---------------------------------------------------------------------------
# Router-based wait tests (S1c-2 / H14: uses router socket, not state file)
# ---------------------------------------------------------------------------

def test_nelix_wait_requires_owner_id(tmp_path):
    """--owner-id is REQUIRED."""
    proc = _run_wait(args=["--orchestration-id", "orch-1"])
    assert proc.returncode != 0


def test_nelix_wait_requires_orchestration_id(tmp_path):
    """--orchestration-id is REQUIRED."""
    proc = _run_wait(args=["--owner-id", "owner-1"])
    assert proc.returncode != 0


def test_nelix_wait_exits_cleanly_when_router_unreachable(tmp_path):
    """When the router socket is absent, exit cleanly with {"kind":"none"}."""
    # Point to a router home with no running router
    env = {"PATH": "/usr/bin:/bin", "NELIX_HOME": str(tmp_path)}
    proc = _run_wait(env=env,
                     args=["--owner-id", "test-owner", "--orchestration-id", "orch-1"])
    assert proc.returncode == 0
    assert json.loads(proc.stdout.strip()) == {"kind": "none"}


def test_nelix_wait_prints_doorbell_from_router(tmp_path):
    """Set up NELIX_HOME with fake router, run nelix-wait, verify doorbell."""
    import paths, importlib
    importlib.reload(paths)

    port, srv = _start_fake_router({
        "event": {
            "session_id": "s-001", "seq": 42, "kind": "waiting_for_user",
            "requires_response": True, "hung": False,
            "summary": "chrome", "screen_excerpt": "big content",
        },
        "cursor": "test-cursor-value",
    })

    try:
        env = {
            "PATH": "/usr/bin:/bin",
            "NELIX_HOME": str(tmp_path),
            # Override router discovery to use TCP
            "NELIX_ROUTER_HOST": "127.0.0.1",
            "NELIX_ROUTER_PORT": str(port),
        }
        proc = _run_wait(env=env,
                         args=["--owner-id", "test-owner", "--orchestration-id", "orch-1"])
        assert proc.returncode == 0
        rec = json.loads(proc.stdout.strip())
        assert rec["schema"] == "nelix.wake.v1"
        assert rec["status_required"] is True
        assert rec["session_id"] == "s-001"
        assert rec["seq"] == 42
        assert rec["kind"] == "waiting_for_user"
        assert rec["requires_response"] is True
        assert "cursor" in rec
        # Doorbell fields only — no executor chrome
        assert "summary" not in rec
        assert "screen_excerpt" not in rec
    finally:
        srv.shutdown()


def test_nelix_wait_handles_cursor_expired(tmp_path):
    """Router sends cursor_expired marker -> waiter returns it."""
    port, srv = _start_fake_router({"event": None, "cursor_expired": True})

    try:
        env = {
            "PATH": "/usr/bin:/bin",
            "NELIX_HOME": str(tmp_path),
            "NELIX_ROUTER_HOST": "127.0.0.1",
            "NELIX_ROUTER_PORT": str(port),
        }
        proc = _run_wait(env=env,
                         args=["--owner-id", "test-owner", "--orchestration-id", "orch-1"])
        assert proc.returncode == 0
        rec = json.loads(proc.stdout.strip())
        assert rec["cursor_expired"] is True
        assert rec["status_required"] is True
    finally:
        srv.shutdown()


def test_nelix_wait_handles_board_changed(tmp_path):
    """Router sends board_changed marker -> waiter returns it."""
    port, srv = _start_fake_router({"event": None, "board_changed": True})

    try:
        env = {
            "PATH": "/usr/bin:/bin",
            "NELIX_HOME": str(tmp_path),
            "NELIX_ROUTER_HOST": "127.0.0.1",
            "NELIX_ROUTER_PORT": str(port),
        }
        proc = _run_wait(env=env,
                         args=["--owner-id", "test-owner", "--orchestration-id", "orch-1"])
        assert proc.returncode == 0
        rec = json.loads(proc.stdout.strip())
        assert rec["board_changed"] is True
        assert rec["status_required"] is True
    finally:
        srv.shutdown()


def test_nelix_wait_doorbell_omits_executor_output(tmp_path):
    """Doorbell carries only metadata, never executor output."""
    port, srv = _start_fake_router({
        "event": {
            "seq": 11, "session_id": "s1", "kind": "blocked",
            "requires_response": True, "hung": False,
            "summary": "box-chrome",
            "screen_excerpt": "> 1. Yes",
        },
        "cursor": "c1",
    })

    try:
        env = {
            "PATH": "/usr/bin:/bin",
            "NELIX_HOME": str(tmp_path),
            "NELIX_ROUTER_HOST": "127.0.0.1",
            "NELIX_ROUTER_PORT": str(port),
        }
        proc = _run_wait(env=env,
                         args=["--owner-id", "test-owner", "--orchestration-id", "orch-1"])
        assert proc.returncode == 0
        rec = json.loads(proc.stdout.strip())
        assert rec["kind"] == "blocked"
        assert rec["requires_response"] is True
        assert "screen_excerpt" not in rec
        assert "summary" not in rec
    finally:
        srv.shutdown()


def test_nelix_wait_doorbell_stays_small_with_huge_screen_excerpt(tmp_path):
    """Wake stays tiny so host notify channel never slices triage fields away."""
    big = "x" * 8000
    port, srv = _start_fake_router({
        "event": {
            "seq": 42, "session_id": "s1", "kind": "waiting_for_user",
            "requires_response": True, "hung": False,
            "summary": "chrome", "screen_excerpt": big,
        },
        "cursor": "c2",
    })

    try:
        env = {
            "PATH": "/usr/bin:/bin",
            "NELIX_HOME": str(tmp_path),
            "NELIX_ROUTER_HOST": "127.0.0.1",
            "NELIX_ROUTER_PORT": str(port),
        }
        proc = _run_wait(env=env,
                         args=["--owner-id", "test-owner", "--orchestration-id", "orch-1"])
        assert len(proc.stdout) < 300, f"doorbell too big ({len(proc.stdout)} bytes)"
        assert big not in proc.stdout
        rec = json.loads(proc.stdout.strip())
        assert rec["session_id"] == "s1" and rec["seq"] == 42
        assert rec["status_required"] is True
    finally:
        srv.shutdown()


def test_nelix_wait_graceful_interrupt(tmp_path):
    """SIGINT while waiting → exits 0 with {"kind":"none"}."""
    hit = threading.Event()

    class BlockingRouter(BaseHTTPRequestHandler):
        def do_GET(self):
            hit.set()
            body = json.dumps({"event": None, "cursor": "c"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            import time; time.sleep(30)

        def do_POST(self):
            return self.do_GET()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), BlockingRouter)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    proc = None
    try:
        env = {
            "PATH": "/usr/bin:/bin",
            "NELIX_HOME": str(tmp_path),
            "NELIX_ROUTER_HOST": "127.0.0.1",
            "NELIX_ROUTER_PORT": str(port),
        }
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bin" / "nelix-wait"),
             "--owner-id", "test-owner", "--orchestration-id", "orch-1"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
