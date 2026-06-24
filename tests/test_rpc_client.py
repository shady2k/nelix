import threading
from conftest import EXECUTOR
from daemon.events import EventQueue
from daemon.rpc_server import make_server
from rpc_client import RpcClient


class FakeManager:
    def __init__(self): self._events = EventQueue(); self.calls = []
    def start(self, e, t): self.calls.append(("start", e, t)); return "s1"
    def respond(self, s, e, a): self.calls.append(("respond", s, e, a)); return True
    def status(self, sid=None): return {"sessions": {}}
    def stop(self, s): self.calls.append(("stop", s)); return True


def test_rpc_client_roundtrip():
    m = FakeManager()
    srv = make_server(m, token="t", port=8781)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = RpcClient("http://127.0.0.1:8781", "t")
        assert c.start(EXECUTOR, "go")["session_id"] == "s1"
        ok, _ = c.respond("s1", "evt-1", "yes")
        assert ok is True and ("respond", "s1", "evt-1", "yes") in m.calls
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


def test_rpc_client_dialog():
    m = FakeManagerDialog()
    srv = make_server(m, token="t", port=8782)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = RpcClient("http://127.0.0.1:8782", "t")
        d = c.dialog("s1", turn=1, offset=2)
        assert d["turn_index"] == 1 and d["text"] == "t1@2"
        assert c.dialog("s1")["turn_index"] == 2          # default -> latest
    finally:
        srv.shutdown()
