import threading
from conftest import EXECUTOR
from daemon.events import EventQueue
from daemon.rpc_server import make_server
from daemon.session import RespondOutcome
from daemon.transport import Transport
from rpc_client import RpcClient


class FakeManager:
    def __init__(self): self._events = EventQueue(); self.calls = []
    def start(self, e, t, c): self.calls.append(("start", e, t, c)); return "s1", 0
    def respond(self, s, a, decision_id=None):
        self.calls.append(("respond", s, a, decision_id))
        return RespondOutcome("resumed", seq=3, decision_id="dec-x")
    def status(self, sid=None): return {"sessions": {}}
    def stop(self, s): self.calls.append(("stop", s)); return True


def test_rpc_client_roundtrip():
    m = FakeManager()
    srv = make_server(m, Transport.tcp("127.0.0.1", 8781, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = RpcClient("http://127.0.0.1:8781", "t")
        assert c.start(EXECUTOR, "go", "/repo")["session_id"] == "s1"
        assert ("start", EXECUTOR, "go", "/repo") in m.calls
        ok, body = c.respond("s1", "yes")
        assert ok is True and ("respond", "s1", "yes", None) in m.calls
        assert body["decision_id"] == "dec-x"
        assert c.stop("s1")["stopped"] is True
    finally:
        srv.shutdown()


class _Dialog:
    def turn_count(self): return 3
    def turn_text(self, turn, offset=0, limit=None):
        return {"turn_index": turn, "text": f"t{turn}@{offset}", "total_len": 4,
                "truncated": False, "unavailable": False}


class _Sess:
    dialog = _Dialog()


class FakeManagerDialog:
    def __init__(self): self._events = EventQueue()
    def status(self, sid=None): return {"sessions": {}}
    def get(self, sid): return _Sess() if sid == "s1" else None


def test_rpc_client_dialog(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))   # isolate from real on-disk sessions
    m = FakeManagerDialog()
    srv = make_server(m, Transport.tcp("127.0.0.1", 8782, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = RpcClient("http://127.0.0.1:8782", "t")
        d = c.dialog("s1", turn=1, offset=2)
        assert d["turn_index"] == 1 and d["text"] == "t1@2"
        assert c.dialog("s1")["turn_index"] == 2          # default -> latest
    finally:
        srv.shutdown()


def test_client_screen_calls_get_screen(monkeypatch):
    from rpc_client import RpcClient
    c = RpcClient("http://x", "t")
    seen = {}
    monkeypatch.setattr(c, "_call",
                        lambda m, p, body=None: seen.update(m=m, p=p) or (200, {"screen": "S"}))
    assert c.screen("s-1") == {"screen": "S"}
    assert seen == {"m": "GET", "p": "/screen?session_id=s-1"}


def test_client_screen_raw_appends_raw_query(monkeypatch):
    from rpc_client import RpcClient
    c = RpcClient("http://x", "t")
    seen = {}
    monkeypatch.setattr(c, "_call",
                        lambda m, p, body=None: seen.update(m=m, p=p) or (200, {"screen": "R"}))
    assert c.screen("s-1", raw=True) == {"screen": "R"}
    assert seen == {"m": "GET", "p": "/screen?session_id=s-1&raw=1"}


def test_client_screen_force_appends_force_query(monkeypatch):
    from rpc_client import RpcClient
    c = RpcClient("http://x", "t")
    seen = {}
    monkeypatch.setattr(c, "_call",
                        lambda m, p, body=None: seen.update(m=m, p=p) or (200, {"screen": "F"}))
    assert c.screen("s-1", force=True) == {"screen": "F"}
    assert seen == {"m": "GET", "p": "/screen?session_id=s-1&force=1"}
