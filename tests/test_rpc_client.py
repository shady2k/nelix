import threading
from daemon.events import EventQueue
from daemon.rpc_server import make_server
from plugin.rpc_client import RpcClient


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
        assert c.start("claude_zai", "go")["session_id"] == "s1"
        ok, _ = c.respond("s1", "evt-1", "yes")
        assert ok is True and ("respond", "s1", "evt-1", "yes") in m.calls
        assert c.stop("s1")["stopped"] is True
    finally:
        srv.shutdown()
