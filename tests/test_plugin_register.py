import json, threading
from daemon.events import EventQueue
from daemon.rpc_server import make_server
import plugin as nelix_plugin


class FakeCtx:
    profile_name = "local"
    def __init__(self): self.tools = {}; self.commands = {}; self.dispatched = []
    def register_tool(self, name, toolset, schema, handler, description="", is_async=False, **k):
        self.tools[name] = {"schema": schema, "handler": handler, "toolset": toolset}
    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler
    def dispatch_tool(self, tool_name, args, **k):
        self.dispatched.append((tool_name, args)); return "{}"


class FakeManager:
    def __init__(self): self._events = EventQueue()
    def start(self, e, t): return "s1"
    def respond(self, s, e, a): return True
    def status(self, sid=None): return {"sessions": {}}
    def stop(self, s): return True


def test_register_wires_four_tools_and_command(monkeypatch):
    srv = make_server(FakeManager(), token="t", port=8782)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NELIX_RPC", "http://127.0.0.1:8782")
    monkeypatch.setenv("NELIX_RPC_TOKEN", "t")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    ctx = FakeCtx()
    try:
        nelix_plugin.register(ctx)
        assert set(ctx.tools) == {"nelix_start", "nelix_status", "nelix_respond", "nelix_stop"}
        assert "nelix" in ctx.commands
        # drive nelix_start handler: it should call /start and arm the waiter
        out = ctx.tools["nelix_start"]["handler"]({"executor": "claude_zai", "task": "go"})
        assert json.loads(out)["session_id"] == "s1"
        assert ctx.dispatched and ctx.dispatched[0][0] == "terminal"
    finally:
        srv.shutdown()
