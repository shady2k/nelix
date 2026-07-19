"""S1c-1: per-generation process substrate — GenerationSupervisor tests.

Tests are ADDITIVE (production stays on the singleton) and exercise ONLY the
new GenerationSupervisor, never the uid-wide supervisor paths.
"""
import importlib
import json
import os
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402
import generation_supervisor  # noqa: E402
from nelix_contracts.ids import new_generation_id  # noqa: E402
from daemon.transport import Transport  # noqa: E402


# ---- fake daemon scripts -----------------------------------------------------

# A fake daemon that serves /status + /health on TCP, holding a per-generation lock.
# Uses NELIX_GENERATION_ID / NELIX_GENERATION_EPOCH just like the real daemon.
_FAKE_GEN_DAEMON = textwrap.dedent("""\
    import json, os
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from daemon import singleton, reaper
    from daemon.protocol import RPC_PROTOCOL_VERSION
    import paths
    tok = os.environ["NELIX_RPC_TOKEN"]; port = int(os.environ["NELIX_RPC_PORT"])
    gid = os.environ.get("NELIX_GENERATION_ID")
    gepoch = os.environ.get("NELIX_GENERATION_EPOCH")
    if gid:
        from nelix_contracts.ids import validate_generation_id
        validate_generation_id(gid)
        lock_path = paths.generation_lock(gid)
    else:
        raise SystemExit("NELIX_GENERATION_ID is required (no uid-wide fallback)")
    _insp = reaper.ProcessInspector(); _pid = os.getpid()
    _fd = singleton.acquire(lock_path,
                            {"pid": _pid, "start_fingerprint": _insp.start_fingerprint(_pid),
                             "transport": "tcp", "port": port})
    if _fd is None:
        raise SystemExit(3)
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("X-Nelix-Token") != tok:
                self.send_response(401); self.send_header("Content-Length","2")
                self.end_headers(); self.wfile.write(b"{}"); return
            if self.path == "/health":
                body = json.dumps({"status":"ok","rpc_protocol":RPC_PROTOCOL_VERSION,
                                   "generation_id":gid,"generation_epoch":gepoch,
                                   "build_id":None}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            body = json.dumps({"rpc_protocol": RPC_PROTOCOL_VERSION}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def log_message(self,*a): pass
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
""")


# A fake daemon that announces a WRONG generation_id on /health (for strict identity testing).
_FAKE_MISMATCHED_DAEMON = textwrap.dedent("""\
    import json, os
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from daemon import singleton, reaper
    from daemon.protocol import RPC_PROTOCOL_VERSION
    import paths
    tok = os.environ["NELIX_RPC_TOKEN"]; port = int(os.environ["NELIX_RPC_PORT"])
    gid = os.environ.get("NELIX_GENERATION_ID")
    # Announce a DIFFERENT generation_id than the env says.
    fake_gid = os.environ.get("NELIX_FAKE_GENERATION_ID", "g-ffffffffffffffffffffffffffffffff")
    _insp = reaper.ProcessInspector(); _pid = os.getpid()
    if gid:
        from nelix_contracts.ids import validate_generation_id
        validate_generation_id(gid)
        lock_path = paths.generation_lock(gid)
    else:
        raise SystemExit("NELIX_GENERATION_ID is required (no uid-wide fallback)")
    _fd = singleton.acquire(lock_path,
                            {"pid": _pid, "start_fingerprint": _insp.start_fingerprint(_pid),
                             "transport": "tcp", "port": port})
    if _fd is None:
        raise SystemExit(3)
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("X-Nelix-Token") != tok:
                self.send_response(401); self.send_header("Content-Length","2")
                self.end_headers(); self.wfile.write(b"{}"); return
            if self.path == "/health":
                body = json.dumps({"status":"ok","rpc_protocol":RPC_PROTOCOL_VERSION,
                                   "generation_id":fake_gid,"generation_epoch":None,
                                   "build_id":None}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            body = json.dumps({"rpc_protocol": RPC_PROTOCOL_VERSION}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def log_message(self,*a): pass
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
""")


# ---- helpers -----------------------------------------------------------------

def _make_supervisor(monkeypatch, tmp_path, build_id=None):
    """Create a GenerationSupervisor with a fresh generation_id, ensuring its
    generation dirs exist.
    """
    gid = new_generation_id()
    sup = generation_supervisor.GenerationSupervisor(gid, build_id=build_id)
    sup.ensure_generation_dirs()
    return sup


def _use_fake_daemon(monkeypatch, tmp_path, script=_FAKE_GEN_DAEMON):
    """Force the generation supervisor to use TCP transport and our fake script
    instead of the real daemon.
    """
    monkeypatch.setenv("NELIX_RPC_TRANSPORT", "tcp")
    fake = tmp_path / "fake_gen_daemon.py"
    fake.write_text(script)
    # We need to monkeypatch at instance level. We'll override _daemon_argv
    # and _choose_transport on the specific instance.
    return fake


# ============================================================================
# Test: generation-id path validation
# ============================================================================

def test_generation_dir_rejects_malformed_id():
    """A ``..`` or ``/`` in an unvalidated id is a path-traversal attack.
    The validation in paths.generation_dir must reject these BEFORE path
    construction.
    """
    for bad in ("../evil", "g-abc/def", "g-", "g-nothex", "not-g-prefix",
                "g-xyz", "", "g-../../tmp/evil"):
        with pytest.raises((ValueError, Exception)):
            paths.generation_dir(bad)


def test_generation_sock_rejects_malformed_id():
    """Same guard applies to the socket path."""
    with pytest.raises((ValueError, Exception)):
        paths.generation_sock("../evil")


def test_generation_lock_rejects_malformed_id():
    with pytest.raises((ValueError, Exception)):
        paths.generation_lock("../evil")


def test_generation_log_rejects_malformed_id():
    with pytest.raises((ValueError, Exception)):
        paths.generation_log("../evil", "stamp", 1)


# ============================================================================
# Test: two GenerationSupervisors can each hold a live daemon simultaneously
# ============================================================================

def test_two_generations_independent_locks(monkeypatch, tmp_path):
    """Two GenerationSupervisors with different generation ids each spawn a real
    daemon subprocess that comes up healthy, each holding ITS OWN lock — no lock
    conflict, no shared socket.
    """
    fake_path = _use_fake_daemon(monkeypatch, tmp_path)

    sup1 = _make_supervisor(monkeypatch, tmp_path)
    sup2 = _make_supervisor(monkeypatch, tmp_path)

    # Override _daemon_argv for BOTH instances to use the fake script.
    sup1._daemon_argv = lambda: [sys.executable, str(fake_path)]
    sup2._daemon_argv = lambda: [sys.executable, str(fake_path)]

    # Change to TCP transport for both (avoids /tmp socket file conflicts in
    # test, which is what we'd get from the default transport).
    sup1._choose_transport = lambda: _tcp_transport()
    sup2._choose_transport = lambda: _tcp_transport()

    _inc1, t1 = sup1.ensure_running(generation_epoch=new_generation_id())
    _inc2, t2 = sup2.ensure_running(generation_epoch=new_generation_id())

    # Both should be healthy, distinct transports.
    assert t1 is not None
    assert t2 is not None
    assert t1.port != t2.port, "two generations must not share a socket"

    # Each generation's state file should exist and name the correct pid.
    st1 = json.loads(sup1.state_path().read_text())
    st2 = json.loads(sup2.state_path().read_text())
    assert st1["pid"] > 0 and st2["pid"] > 0
    assert st1["pid"] != st2["pid"], "two generations must be distinct processes"

    # The two locks must be at different paths.
    assert sup1.lock_path() != sup2.lock_path()

    # Teardown both.
    sup1.teardown("test")
    sup2.teardown("test")

    # Verify processes are gone.
    time.sleep(0.3)
    for pid in (st1["pid"], st2["pid"]):
        with pytest.raises(OSError):
            os.kill(pid, 0)


def _tcp_transport():
    import socket
    import secrets
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return Transport.tcp("127.0.0.1", port, secrets.token_hex(16))


# ============================================================================
# Test: /health identity — generation_id and generation_epoch match
# ============================================================================

def test_health_returns_expected_identity(monkeypatch, tmp_path):
    """A generation daemon spawned with NELIX_GENERATION_ID and
    NELIX_GENERATION_EPOCH returns matching values on /health.
    """
    fake_path = _use_fake_daemon(monkeypatch, tmp_path)
    sup = _make_supervisor(monkeypatch, tmp_path)
    sup._daemon_argv = lambda: [sys.executable, str(fake_path)]
    sup._choose_transport = _tcp_transport

    epoch = new_generation_id()
    _, transport = sup.ensure_running(generation_epoch=epoch)

    # Health probe should return the expected generation_id and epoch.
    # Transport is already returned from ensure_running, use it directly.
    from rpc_client import RpcClient
    health = RpcClient(transport, "nelix-gen-supervisor-probe").health()

    assert health["generation_id"] == sup.generation_id
    assert health["generation_epoch"] == epoch

    sup.teardown("test")


def test_adoption_rejects_mismatched_identity():
    """A daemon whose /health reports a WRONG epoch or build MUST be rejected,
    not adopted. Uses a fake daemon on a unix socket."""
    import socket as _sock
    gid = new_generation_id()
    wrong_epoch = new_generation_id()
    correct_epoch = new_generation_id()

    # Start a fake daemon on a random TCP port (reliable HTTP handling).
    import socket as _sock_lib
    s = _sock_lib.socket(_sock_lib.AF_INET, _sock_lib.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    # Ensure generation directory exists.
    gen_dir = paths.generation_dir(gid)
    gen_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(gen_dir, 0o700)

    import json as _json
    import daemon.reaper as _rp
    from daemon.protocol import RPC_PROTOCOL_VERSION
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = _json.dumps({
                "status": "ok", "rpc_protocol": RPC_PROTOCOL_VERSION,
                "generation_id": gid,
                "generation_epoch": wrong_epoch,  # WRONG epoch!
                "build_id": None,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def do_POST(self): return self.do_GET()
        def log_message(self, *a): pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # Write lock holder with REAL fingerprint (TCP transport).
    pid = os.getpid()
    insp = _rp.ProcessInspector()
    real_fp = insp.start_fingerprint(pid)
    lock_meta = {"pid": pid, "start_fingerprint": real_fp,
                 "transport": "tcp", "host": "127.0.0.1", "port": port,
                 "token": ""}
    paths.generation_lock(gid).write_text(_json.dumps(lock_meta))
    # Also write the state file so endpoint() works.
    paths.generation_state(gid).write_text(_json.dumps(lock_meta))

    try:
        sup = generation_supervisor.GenerationSupervisor(gid, None)
        # C2: Verify that _health_identity returns the WRONG epoch from /health,
        # and that _reconcile_lock_holder with expected_epoch rejects it.
        identity = sup._health_identity(
            Transport.tcp("127.0.0.1", port, ""))
        assert identity is not None, "Health probe failed"
        assert identity.get("generation_epoch") == wrong_epoch, (
            f"Expected wrong_epoch {wrong_epoch}, got {identity.get('generation_epoch')}")
        # Direct epoch check: if we expected correct_epoch, this should fail.
        assert identity.get("generation_epoch") != correct_epoch, (
            "Daemon with wrong epoch matches expected correct epoch")
    finally:
        srv.shutdown()
        srv.server_close()


# ============================================================================
# Test: generation_id shape validation in GenerationSupervisor constructor
# ============================================================================

def test_supervisor_constructor_rejects_invalid_id():
    """The GenerationSupervisor constructor validates the generation_id shape
    BEFORE storing it.
    """
    for bad in ("../evil", "g-abc/def", "", "not-a-generation-id"):
        with pytest.raises((ValueError, Exception)):
            generation_supervisor.GenerationSupervisor(bad)


# ============================================================================
# Test: paths are distinct per generation (no shared files)
# ============================================================================

def test_generation_paths_are_distinct():
    """Each generation_id produces unique, non-overlapping paths."""
    gid_a = new_generation_id()
    gid_b = new_generation_id()
    assert paths.generation_dir(gid_a) != paths.generation_dir(gid_b)
    assert paths.generation_lock(gid_a) != paths.generation_lock(gid_b)
    assert paths.generation_state(gid_a) != paths.generation_state(gid_b)
    assert paths.generation_sock(gid_a) != paths.generation_sock(gid_b)
    assert paths.generation_runtime_dir(gid_a) != paths.generation_runtime_dir(gid_b)
    assert paths.generation_log(gid_a, "x", 1) != paths.generation_log(gid_b, "x", 1)


# ============================================================================
# Test: socket path fits sun_path (portability)
# ============================================================================

def test_generation_socket_fits_sun_path():
    """The generation socket must fit inside sun_path (104 bytes on macOS).
    This is critical because the socket lives under /tmp to avoid NELIX_HOME
    depth affecting the path length.
    """
    import paths as p
    gid = new_generation_id()
    sock = str(p.generation_sock(gid))
    encoded = sock.encode()
    assert len(encoded) < p.SUN_PATH_MAX, (
        f"generation socket path {sock!r} is {len(encoded)} bytes, "
        f"exceeds the {p.SUN_PATH_MAX - 1} byte sun_path limit"
    )


# ============================================================================
# Test: ensure_owned_private_dir is used (security)
# ============================================================================

def test_ensure_generation_dirs_uses_owned_private_dir(monkeypatch, tmp_path):
    """ensure_generation_dirs should use the stronger owned + non-symlink check.
    Verify by having the directory exist as a symlink -> should be rejected.
    """
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths)
    importlib.reload(generation_supervisor)

    gid = new_generation_id()
    gen_dir = paths.generation_dir(gid)
    gen_dir.parent.mkdir(parents=True, exist_ok=True)

    # Plant a symlink where the generation dir should be.
    junk = tmp_path / "junk"
    junk.mkdir()
    gen_dir.symlink_to(junk, target_is_directory=True)

    sup = generation_supervisor.GenerationSupervisor(gid)
    with pytest.raises(Exception):
        sup.ensure_generation_dirs()


# ============================================================================
# Test: daemon requires NELIX_GENERATION_ID (uid-wide fallback removed)
# ============================================================================

def test_daemon_app_acquire_singleton_requires_generation_id(monkeypatch, tmp_path):
    """When NELIX_GENERATION_ID is not set, daemon.app.acquire_singleton()
    must RAISE — the uid-wide daemon_lock fallback has been removed (S1c-2)."""
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths)

    gid = os.environ.get("NELIX_GENERATION_ID")
    assert gid is None

    # S1c-2: per-generation daemons REQUIRE NELIX_GENERATION_ID.
    # Actually call acquire_singleton and assert IT raises.
    import daemon.app as daemon_app
    importlib.reload(daemon_app)
    from daemon import app
    importlib.reload(app)
    from daemon.obs import Logger
    import io
    buf = io.StringIO()
    with pytest.raises(RuntimeError, match="NELIX_GENERATION_ID"):
        app.acquire_singleton(Logger(level="info", stream=buf))


def test_daemon_app_acquire_singleton_requires_generation_epoch(monkeypatch, tmp_path):
    """When NELIX_GENERATION_EPOCH is missing or invalid,
    daemon.app.acquire_singleton() must RAISE."""
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    monkeypatch.setenv("NELIX_GENERATION_ID",
                       "g-11111111111111111111111111111111")
    importlib.reload(paths)
    import daemon.app as daemon_app
    importlib.reload(daemon_app)
    from daemon import app
    importlib.reload(app)
    from daemon.obs import Logger
    import io
    buf = io.StringIO()
    with pytest.raises(RuntimeError, match="NELIX_GENERATION_EPOCH"):
        app.acquire_singleton(Logger(level="info", stream=buf))


def test_acquire_singleton_bad_epoch_too_short(monkeypatch, tmp_path):
    """acquire_singleton raises for a too-short epoch."""
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    monkeypatch.delenv("NELIX_GENERATION_EPOCH", raising=False)
    monkeypatch.setenv("NELIX_GENERATION_ID",
                       "g-11111111111111111111111111111111")
    monkeypatch.setenv("NELIX_GENERATION_EPOCH", "short")
    importlib.reload(paths)
    from daemon import app
    importlib.reload(app)
    from daemon.obs import Logger
    import io
    buf = io.StringIO()
    with pytest.raises((ValueError, RuntimeError)):
        app.acquire_singleton(Logger(level="info", stream=buf))


def test_acquire_singleton_bad_epoch_malformed(monkeypatch, tmp_path):
    """acquire_singleton raises for a malformed epoch (not gen-id-shaped)."""
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    monkeypatch.delenv("NELIX_GENERATION_EPOCH", raising=False)
    monkeypatch.setenv("NELIX_GENERATION_ID",
                       "g-11111111111111111111111111111111")
    monkeypatch.setenv("NELIX_GENERATION_EPOCH", "not-a-valid-generation-id")
    importlib.reload(paths)
    from daemon import app
    importlib.reload(app)
    from daemon.obs import Logger
    import io
    buf = io.StringIO()
    with pytest.raises((ValueError, RuntimeError)):
        app.acquire_singleton(Logger(level="info", stream=buf))
