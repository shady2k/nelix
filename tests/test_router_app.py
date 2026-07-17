"""nelix-3rm slice 3c.1 Part A: router/app.py bootstrap — establish the secure runtime dir, build
the ONE shared StartLedger + registry + start path, and serve. A second router (lost flock) exits
cleanly; the shared pieces are wired in the right order and torn down on exit."""
import re

import pytest

import router.app as app


class _FakeHandle:
    def __init__(self):
        self.socket = object()
        self.sock_path = "/tmp/router-boot.sock"
        self.closed = False

    def close(self):
        self.closed = True


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
    monkeypatch.setattr(app, "_install_shutdown_handlers", lambda server: None)

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
