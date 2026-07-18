import hashlib
import os
import threading

import pytest

import paths
from conftest import EXECUTOR, OWNER
from daemon import owner
from daemon.events import EventQueue
from daemon.manager import StartOutcome, StopOutcome
from daemon.rpc_server import make_server
from daemon.session import RespondOutcome
from daemon.transport import Transport
from rpc_client import RpcClient


class FakeManager:
    def __init__(self): self._events = EventQueue(); self.calls = []
    def start(self, e, t, c, *, owner_id, model=None, session_id=None):
        self.calls.append(("start", e, t, c))
        return StartOutcome(session_id="s-00000001", base_seq=0,
                            snapshot={"session_id": "s-00000001", "control_state": "busy",
                                      "task_delivery": "pending", "pending": False})
    def respond(self, s, a, *, owner_id, decision_id=None):
        self.calls.append(("respond", s, a, decision_id))
        return RespondOutcome("resumed", seq=3, decision_id="dec-x")
    def status(self, sid=None, *, owner_id, include_progress=False): return {"sessions": {}}
    def stop(self, s, *, owner_id):
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
        c = RpcClient(Transport.tcp("127.0.0.1", 8781, "t"), OWNER)
        assert c.start(EXECUTOR, "go", "/repo")["session_id"] == "s-00000001"
        assert ("start", EXECUTOR, "go", "/repo") in m.calls
        ok, body = c.respond("s-00000001", "yes")
        assert ok is True and ("respond", "s-00000001", "yes", None) in m.calls
        assert body["decision_id"] == "dec-x"
        assert c.stop("s-00000001")["status"] == "stopped"
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
    def status(self, sid=None, *, owner_id, include_progress=False): return {"sessions": {}}
    def get(self, sid): return _Sess() if sid == "s-00000001" else None


def test_rpc_client_dialog(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))   # isolate from real on-disk sessions
    # /dialog is owner-gated and reads the DURABLE record, so the session needs one — a fake
    # manager cannot stand in for it. That is the gate working: /dialog reads the transcript off
    # disk without consulting the manager at all, so the record is the only thing between a
    # session id and someone else's transcript.
    owner.write(paths.sessions_root() / "s-00000001", OWNER)
    m = FakeManagerDialog()
    srv = make_server(m, Transport.tcp("127.0.0.1", 8782, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = RpcClient(Transport.tcp("127.0.0.1", 8782, "t"), OWNER)
        # Offset-based pagination (no turn parameter)
        d = c.dialog("s-00000001", offset=42)
        assert d["text"] == "transcript@42"
        assert "speaker_at_start" in d          # flat-log field present
        d2 = c.dialog("s-00000001")                     # default offset=0
        assert d2["text"] == "transcript@0"
    finally:
        srv.shutdown()


def test_client_start_omits_model_when_not_provided(monkeypatch):
    # nelix-9k0: a default (no-model) call is byte-for-byte the same POST body as before this
    # feature — "model" is only added when provided (mirrors status(include_progress=...)). start()
    # forwards through the phase-split _forward_call now, so that is the patch point.
    c = RpcClient(Transport.tcp("x", 80, "t"), OWNER)
    seen = {}
    monkeypatch.setattr(c, "_forward_call",
                        lambda m, p, body, **k: seen.update(m=m, p=p, body=body) or {})
    c.start(EXECUTOR, "go", "/repo")
    assert seen["m"] == "POST" and seen["p"] == "/start"
    assert seen["body"] == {"executor": EXECUTOR, "task": "go", "cwd": "/repo",
                            "owner_id": OWNER}
    assert "model" not in seen["body"]


def test_client_start_includes_model_when_provided(monkeypatch):
    c = RpcClient(Transport.tcp("x", 80, "t"), OWNER)
    seen = {}
    monkeypatch.setattr(c, "_forward_call",
                        lambda m, p, body, **k: seen.update(body=body) or {})
    c.start(EXECUTOR, "go", "/repo", model="haiku")
    assert seen["body"] == {"executor": EXECUTOR, "task": "go", "cwd": "/repo", "model": "haiku",
                            "owner_id": OWNER}


def test_client_screen_calls_get_screen(monkeypatch):
    from rpc_client import RpcClient
    c = RpcClient(Transport.tcp("x", 80, "t"), OWNER)
    seen = {}
    monkeypatch.setattr(c, "_call",
                        lambda m, p, body=None: seen.update(m=m, p=p) or (200, {"screen": "S"}))
    assert c.screen("s-1") == {"screen": "S"}
    assert seen == {"m": "GET", "p": f"/screen?session_id=s-1&owner_id={OWNER}"}


def test_client_screen_raw_appends_raw_query(monkeypatch):
    from rpc_client import RpcClient
    c = RpcClient(Transport.tcp("x", 80, "t"), OWNER)
    seen = {}
    monkeypatch.setattr(c, "_call",
                        lambda m, p, body=None: seen.update(m=m, p=p) or (200, {"screen": "R"}))
    assert c.screen("s-1", raw=True) == {"screen": "R"}
    assert seen == {"m": "GET", "p": f"/screen?session_id=s-1&owner_id={OWNER}&raw=1"}


def test_client_screen_force_appends_force_query(monkeypatch):
    from rpc_client import RpcClient
    c = RpcClient(Transport.tcp("x", 80, "t"), OWNER)
    seen = {}
    monkeypatch.setattr(c, "_call",
                        lambda m, p, body=None: seen.update(m=m, p=p) or (200, {"screen": "F"}))
    assert c.screen("s-1", force=True) == {"screen": "F"}
    assert seen == {"m": "GET", "p": f"/screen?session_id=s-1&owner_id={OWNER}&force=1"}


def test_client_roundtrips_over_unix_socket(unix_sock, fake_manager):
    server = make_server(fake_manager, Transport.unix(unix_sock))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = RpcClient(Transport.unix(unix_sock), OWNER)
        body = client.status()          # GET /status over the unix socket, no token
        assert isinstance(body, dict)   # whatever fake_manager.status(None) returns
    finally:
        server.shutdown(); server.server_close()
