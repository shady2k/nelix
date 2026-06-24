import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugin_loader import load_plugin  # noqa: E402

_FAKE = textwrap.dedent("""
    import os, json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    tok=os.environ['NELIX_RPC_TOKEN']; port=int(os.environ['NELIX_RPC_PORT'])
    class H(BaseHTTPRequestHandler):
        def _j(self,o):
            b=json.dumps(o).encode(); self.send_response(200)
            self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if self.headers.get('X-Nelix-Token')!=tok:
                self.send_response(401); self.send_header('Content-Length','2'); self.end_headers(); self.wfile.write(b'{}'); return
            self._j({'sessions': {}, 'limit': 1})
        def do_POST(self):
            n=int(self.headers.get('Content-Length',0)); self.rfile.read(n)
            self._j({'session_id':'s1'})
        def log_message(self,*a): pass
    ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
""")


class FakeCtx:
    profile_name = "local"
    def __init__(self):
        self.tools={}; self.commands={}; self.skills={}; self.hooks={}; self.dispatched=[]
    def register_tool(self, name, toolset, schema, handler, description="", **k):
        self.tools[name]={"schema":schema,"handler":handler,"description":description}
    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name]=handler
    def register_skill(self, name, path, description=""):
        self.skills[name]={"path":path}
    def register_hook(self, name, cb):
        self.hooks[name]=cb
    def dispatch_tool(self, name, args, **k):
        self.dispatched.append((name,args)); return "{}"


def _load_with_fake(monkeypatch, tmp_path):
    """Seed registry + load the plugin, then point the PLUGIN'S supervisor at a
    fake daemon. Must patch nelix.supervisor (the plugin uses
    hermes_plugins.nelix.supervisor — a different object from a top-level
    `import supervisor`)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = tmp_path/"nelix"/"nelix.toml"; cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[executors.opencode]\ncommand="opencode"\nargs=[]\nenv={}\ncwd="."\ndriver="claude"\nlauncher="local"\n')
    fake = tmp_path/"fake_daemon.py"; fake.write_text(_FAKE)
    nelix = load_plugin()
    monkeypatch.setattr(nelix.supervisor, "_daemon_argv", lambda: [sys.executable, str(fake)])
    monkeypatch.setattr(nelix, "resolve_launcher", lambda *a, **k: "local")
    return nelix


def test_register_wires_tools_command_skill_hook(monkeypatch, tmp_path):
    nelix = _load_with_fake(monkeypatch, tmp_path)
    ctx = FakeCtx()
    try:
        nelix.register(ctx)
        assert set(ctx.tools) == {"nelix_start","nelix_status","nelix_respond","nelix_stop"}
        assert "nelix" in ctx.commands
        assert "nelix-orchestration" in ctx.skills
        assert "on_session_end" in ctx.hooks
        # description lives INSIDE the schema (Hermes' LLM builder reads schema, not the
        # description= kwarg) — see test_tool_schemas_are_llm_function_shaped.
        assert "opencode" in ctx.tools["nelix_start"]["schema"]["description"]
        assert "skill_view" in ctx.tools["nelix_start"]["schema"]["description"]
        out = ctx.tools["nelix_start"]["handler"]({"executor":"opencode","task":"go"})
        assert json.loads(out)["session_id"] == "s1"
        assert ctx.dispatched and ctx.dispatched[0][0] == "terminal"
    finally:
        nelix.supervisor.teardown()


def test_tool_schemas_are_llm_function_shaped(monkeypatch, tmp_path):
    """Each tool's schema must be a full function schema (description + parameters),
    because Hermes builds the LLM tool spec as {**schema, "name": name} (tools/
    registry.py) — a bare parameters object + description= kwarg is dropped and the
    model sees an undescribed, paramless tool."""
    nelix = _load_with_fake(monkeypatch, tmp_path)
    ctx = FakeCtx()
    try:
        nelix.register(ctx)
        for tname in ("nelix_start", "nelix_status", "nelix_respond", "nelix_stop"):
            fn = {**ctx.tools[tname]["schema"], "name": tname}  # mirror Hermes' builder
            assert fn.get("description"), f"{tname}: no description in the LLM function spec"
            params = fn.get("parameters")
            assert isinstance(params, dict) and params.get("type") == "object", \
                f"{tname}: parameters missing/!=object in the LLM function spec"
        start = ctx.tools["nelix_start"]["schema"]["parameters"]
        assert {"executor", "task"} <= set(start["properties"])
        assert start.get("required") == ["executor", "task"]
    finally:
        nelix.supervisor.teardown()


def test_session_end_hook_calls_teardown(monkeypatch, tmp_path):
    nelix = _load_with_fake(monkeypatch, tmp_path)
    called = {}
    monkeypatch.setattr(nelix.supervisor, "teardown", lambda *a, **k: called.setdefault("t", True))
    ctx = FakeCtx()
    nelix.register(ctx)
    ctx.hooks["on_session_end"]()
    assert called.get("t") is True
