import json, threading, textwrap, tempfile, os
from pathlib import Path
from conftest import EXECUTOR
from daemon.events import EventQueue
from daemon.rpc_server import make_server
import plugin as nelix_plugin


class FakeCtx:
    profile_name = "local"
    def __init__(self): self.tools = {}; self.commands = {}; self.dispatched = {}; self.skills = {}
    def register_tool(self, name, toolset, schema, handler, description="", is_async=False, **k):
        self.tools[name] = {"schema": schema, "handler": handler, "toolset": toolset, "description": description}
    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler
    def register_skill(self, name, path, description=""):
        self.skills[name] = {"path": path, "description": description}
    def dispatch_tool(self, tool_name, args, **k):
        self.dispatched.setdefault(tool_name, []).append(args); return "{}"


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
        out = ctx.tools["nelix_start"]["handler"]({"executor": EXECUTOR, "task": "go"})
        assert json.loads(out)["session_id"] == "s1"
        assert "terminal" in ctx.dispatched
    finally:
        srv.shutdown()


_DEMO_TOML = textwrap.dedent("""\
    [executors.demo_cli]
    command = "demo"
    args = []
    env = {}
    cwd = "."
    driver = "claude"
""")


def _make_temp_toml(tmp_path):
    p = tmp_path / "nelix.toml"
    p.write_text(_DEMO_TOML)
    return str(p)


def test_skill_registered(monkeypatch, tmp_path):
    """register() must register the nelix-orchestration skill."""
    srv = make_server(FakeManager(), token="t2", port=8783)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NELIX_RPC", "http://127.0.0.1:8783")
    monkeypatch.setenv("NELIX_RPC_TOKEN", "t2")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("NELIX_CONFIG", _make_temp_toml(tmp_path))
    ctx = FakeCtx()
    try:
        nelix_plugin.register(ctx)
        assert "nelix-orchestration" in ctx.skills, "nelix-orchestration skill must be registered"
        skill_path = ctx.skills["nelix-orchestration"]["path"]
        assert Path(skill_path).exists(), f"SKILL.md not found at {skill_path}"
    finally:
        srv.shutdown()


def test_nelix_start_description_contains_executor_name(monkeypatch, tmp_path):
    """nelix_start description must include the configured executor name."""
    srv = make_server(FakeManager(), token="t3", port=8784)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NELIX_RPC", "http://127.0.0.1:8784")
    monkeypatch.setenv("NELIX_RPC_TOKEN", "t3")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("NELIX_CONFIG", _make_temp_toml(tmp_path))
    ctx = FakeCtx()
    try:
        nelix_plugin.register(ctx)
        desc = ctx.tools["nelix_start"]["description"]
        assert "demo_cli" in desc, f"Expected 'demo_cli' in nelix_start description, got: {desc!r}"
    finally:
        srv.shutdown()
