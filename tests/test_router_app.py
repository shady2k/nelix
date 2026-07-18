"""nelix-3rm slice 3c.1 Part A: router/app.py bootstrap — establish the secure runtime dir, build
the ONE shared StartLedger + registry + start path, and serve. A second router (lost flock) exits
cleanly; the shared pieces are wired in the right order and torn down on exit."""
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import paths
import router.app as app

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _wait_until(pred, timeout, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


class _FakeHandle:
    def __init__(self):
        self.socket = object()
        self.sock_path = "/tmp/router-boot.sock"
        self.closed = False

    def close(self):
        self.closed = True


def test_sigterm_stops_the_router_cleanly_and_releases_the_lock(monkeypatch, tmp_path):
    """Finding #2: SIGTERM must stop a REAL router process WITHOUT hanging — the handler must not
    call server.shutdown() from the serving (main) thread, which waits for serve_forever() to return
    and so deadlocks — and must RELEASE the socket + flock so a fresh establish() re-acquires them.

    The router runs in a subprocess (signals are delivered to its main thread, exactly the deadlock
    condition); a buggy handler leaves it hung, which this test catches as a timeout on SIGTERM."""
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "NELIX_HOME": str(home),
           "PYTHONPATH": str(_REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.Popen([sys.executable, "-c", "import router.app; router.app.main()"],
                            cwd=str(_REPO_ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # Point THIS process's path accessors at the SAME runtime location the child derives.
    monkeypatch.setenv("NELIX_HOME", str(home))
    from router.runtime_dir import establish, RouterLockHeld
    leaf = None
    try:
        sock_path = paths.router_sock()
        leaf = sock_path.parent
        assert _wait_until(lambda: sock_path.exists() and proc.poll() is None, timeout=15), (
            f"router never bound its socket (exit={proc.poll()}, "
            f"output={(proc.stdout.read() or b'').decode(errors='replace') if proc.poll() else ''})")
        # Serving and holding the flock: a fresh establish() must LOSE the lock.
        with pytest.raises(RouterLockHeld):
            establish()
        # SIGTERM -> the child must exit PROMPTLY (no deadlock).
        proc.send_signal(signal.SIGTERM)
        assert _wait_until(lambda: proc.poll() is not None, timeout=10), (
            "router hung on SIGTERM: serve_forever() never returned "
            "(the handler called shutdown() from the serving thread)")
        assert proc.returncode == 0
        # The flock is RELEASED: a fresh establish() now re-acquires it and can be torn down.
        handle = establish()
        handle.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if leaf is not None:
            shutil.rmtree(leaf, ignore_errors=True)


def test_lost_flock_exits_cleanly(monkeypatch):
    def _held():
        raise app.RouterLockHeld("another router holds the lock")
    monkeypatch.setattr(app, "establish", _held)
    with pytest.raises(SystemExit) as ei:
        app.main()
    assert ei.value.code == 3                       # a defined, clean exit — not a traceback


def test_main_wires_shared_pieces_and_serves_then_tears_down(monkeypatch):
    captured = {}
    handle = _FakeHandle()

    class _FakeLedger:
        def __init__(self, root):
            captured["ledger_root"] = root
            self.closed = False

        def close(self):
            self.closed = True

    class _FakeServer:
        def __init__(self):
            self.shutdown_called = False

        def serve_forever(self):
            captured["served"] = True
            raise SystemExit(0)                     # stop main() the moment it starts serving

        def shutdown(self):
            self.shutdown_called = True

    def _fake_make_server(sock, sock_path, start_path, registry, router_epoch):
        captured["server_args"] = (sock, sock_path, start_path, registry, router_epoch)
        return _FakeServer()

    monkeypatch.setattr(app, "establish", lambda: handle)
    monkeypatch.setattr(app, "StartLedger", _FakeLedger)
    monkeypatch.setattr(app, "GenerationRegistry", lambda: "REGISTRY")
    monkeypatch.setattr(app, "make_router_server", _fake_make_server)
    monkeypatch.setattr(app, "_install_shutdown_handlers", lambda: None)

    ledger_box = {}
    real_startpath = app.StartPath
    monkeypatch.setattr(app, "StartPath",
                        lambda ledger, registry: ledger_box.setdefault("sp", real_startpath(ledger, registry)))

    with pytest.raises(SystemExit):
        app.main()

    assert captured["served"] is True
    sock, sock_path, start_path, registry, router_epoch = captured["server_args"]
    assert sock is handle.socket and sock_path == handle.sock_path
    assert registry == "REGISTRY"
    assert re.match(r"^r-[0-9a-f]{32}$", router_epoch)       # a per-process router epoch
    # The ONE shared ledger the registry/start path use is closed, and the runtime dir released.
    assert handle.closed is True
    assert ledger_box["sp"] is start_path                     # the start path is wired to serve
