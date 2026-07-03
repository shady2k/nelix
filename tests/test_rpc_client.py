import hashlib
import os
import threading

import pytest

from conftest import EXECUTOR
from daemon.events import EventQueue
from daemon.manager import StartOutcome, StopOutcome
from daemon.rpc_server import make_server
from daemon.session import RespondOutcome
from daemon.transport import Transport
from rpc_client import RpcClient


class FakeManager:
    def __init__(self): self._events = EventQueue(); self.calls = []
    def start(self, e, t, c):
        self.calls.append(("start", e, t, c))
        return StartOutcome(session_id="s1", base_seq=0,
                            snapshot={"session_id": "s1", "control_state": "busy",
                                      "task_delivery": "pending", "pending": False})
    def respond(self, s, a, decision_id=None):
        self.calls.append(("respond", s, a, decision_id))
        return RespondOutcome("resumed", seq=3, decision_id="dec-x")
    def status(self, sid=None, include_progress=False): return {"sessions": {}}
    def stop(self, s):
        self.calls.append(("stop", s))
        return StopOutcome("stopped", snapshot={"session_id": s,
                                                "control_state": "terminal",
                                                "terminal_kind": "stopped", "pending": False})


@pytest.fixture
def fake_manager():
    return FakeManager()


@pytest.fixture
def unix_sock(tmp_path):
    """Short AF_UNIX socket path (<=103 chars incl. NUL).

    pytest tmp_path on macOS resolves through /private/var/folders/... and easily
    exceeds the 104-byte sun_path limit.  Hash tmp_path for uniqueness; put the
    node directly under /tmp so the total stays ~20 chars.
    """
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:8]
    p = f"/tmp/nxc{h}.sock"
    yield p
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass


def test_rpc_client_roundtrip():
    m = FakeManager()
    srv = make_server(m, Transport.tcp("127.0.0.1", 8781, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = RpcClient(Transport.tcp("127.0.0.1", 8781, "t"))
        assert c.start(EXECUTOR, "go", "/repo")["session_id"] == "s1"
        assert ("start", EXECUTOR, "go", "/repo") in m.calls
        ok, body = c.respond("s1", "yes")
        assert ok is True and ("respond", "s1", "yes", None) in m.calls
        assert body["decision_id"] == "dec-x"
        assert c.stop("s1")["status"] == "stopped"
    finally:
        srv.shutdown()


class _Dialog:
    """Fake dialog exposing the flat-log page() API."""
    available = True

    def page(self, offset=0, limit=None, snap=True):
        text = f"transcript@{offset}"
        return {"text": text, "start_offset": offset, "next_offset": offset + len(text),
                "speaker_at_start": "agent", "continued": False, "total_len": 100}


class _Sess:
    dialog = _Dialog()


class FakeManagerDialog:
    def __init__(self): self._events = EventQueue()
    def status(self, sid=None, include_progress=False): return {"sessions": {}}
    def get(self, sid): return _Sess() if sid == "s1" else None


def test_rpc_client_dialog(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))   # isolate from real on-disk sessions
    m = FakeManagerDialog()
    srv = make_server(m, Transport.tcp("127.0.0.1", 8782, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = RpcClient(Transport.tcp("127.0.0.1", 8782, "t"))
        # Offset-based pagination (no turn parameter)
        d = c.dialog("s1", offset=42)
        assert d["text"] == "transcript@42"
        assert "speaker_at_start" in d          # flat-log field present
        d2 = c.dialog("s1")                     # default offset=0
        assert d2["text"] == "transcript@0"
    finally:
        srv.shutdown()


def test_client_screen_calls_get_screen(monkeypatch):
    from rpc_client import RpcClient
    c = RpcClient(Transport.tcp("x", 80, "t"))
    seen = {}
    monkeypatch.setattr(c, "_call",
                        lambda m, p, body=None: seen.update(m=m, p=p) or (200, {"screen": "S"}))
    assert c.screen("s-1") == {"screen": "S"}
    assert seen == {"m": "GET", "p": "/screen?session_id=s-1"}


def test_client_screen_raw_appends_raw_query(monkeypatch):
    from rpc_client import RpcClient
    c = RpcClient(Transport.tcp("x", 80, "t"))
    seen = {}
    monkeypatch.setattr(c, "_call",
                        lambda m, p, body=None: seen.update(m=m, p=p) or (200, {"screen": "R"}))
    assert c.screen("s-1", raw=True) == {"screen": "R"}
    assert seen == {"m": "GET", "p": "/screen?session_id=s-1&raw=1"}


def test_client_screen_force_appends_force_query(monkeypatch):
    from rpc_client import RpcClient
    c = RpcClient(Transport.tcp("x", 80, "t"))
    seen = {}
    monkeypatch.setattr(c, "_call",
                        lambda m, p, body=None: seen.update(m=m, p=p) or (200, {"screen": "F"}))
    assert c.screen("s-1", force=True) == {"screen": "F"}
    assert seen == {"m": "GET", "p": "/screen?session_id=s-1&force=1"}


def test_client_roundtrips_over_unix_socket(unix_sock, fake_manager):
    server = make_server(fake_manager, Transport.unix(unix_sock))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = RpcClient(Transport.unix(unix_sock))
        body = client.status()          # GET /status over the unix socket, no token
        assert isinstance(body, dict)   # whatever fake_manager.status(None) returns
    finally:
        server.shutdown(); server.server_close()
