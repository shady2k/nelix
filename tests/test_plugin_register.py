import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugin_loader import load_plugin  # noqa: E402
from daemon.transport import Transport  # noqa: E402

_FAKE = textwrap.dedent("""
    import os, json
    import paths
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from daemon import singleton, reaper
    from daemon.protocol import RPC_PROTOCOL_VERSION
    tok=os.environ['NELIX_RPC_TOKEN']; port=int(os.environ['NELIX_RPC_PORT'])
    _insp=reaper.ProcessInspector(); _pid=os.getpid()
    _fd=singleton.acquire(paths.daemon_lock(),
                          {'pid':_pid,'start_fingerprint':_insp.start_fingerprint(_pid),
                           'transport':'tcp','port':port})
    if _fd is None:
        raise SystemExit(3)
    class H(BaseHTTPRequestHandler):
        def _j(self,o):
            b=json.dumps(o).encode(); self.send_response(200)
            self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if self.headers.get('X-Nelix-Token')!=tok:
                self.send_response(401); self.send_header('Content-Length','2'); self.end_headers(); self.wfile.write(b'{}'); return
            self._j({'sessions': {}, 'limit': 1, 'rpc_protocol': RPC_PROTOCOL_VERSION})
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
    monkeypatch.setenv("NELIX_RPC_TRANSPORT", "tcp")
    cfg = tmp_path / "workspace" / "nelix" / "nelix.toml"; cfg.parent.mkdir(parents=True, exist_ok=True)
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
        assert set(ctx.tools) == {"nelix_start","nelix_status","nelix_respond","nelix_stop",
                                  "nelix_restart","nelix_dialog","nelix_screen","nelix_models"}
        assert "nelix" in ctx.commands
        assert "nelix-orchestration" in ctx.skills
        # on_session_finalize (NOT on_session_end): the latter fires per run_conversation
        # (every turn) and would tear the daemon down mid-task; finalize fires only at true
        # teardown (CLI exit / /new / /reset). See test_session_finalize_hook_calls_teardown.
        assert "on_session_finalize" in ctx.hooks
        # description lives INSIDE the schema (Hermes' LLM builder reads schema, not the
        # description= kwarg) — see test_tool_schemas_are_llm_function_shaped.
        assert "opencode" in ctx.tools["nelix_start"]["schema"]["description"]
        assert "skill_view" in ctx.tools["nelix_start"]["schema"]["description"]
        out = ctx.tools["nelix_start"]["handler"]({"executor":"opencode","task":"go"})
        assert json.loads(out)["session_id"] == "s1"
        assert ctx.dispatched and ctx.dispatched[0][0] == "terminal"
        # nelix_dialog reaches the daemon (GET /dialog) and returns a JSON object
        dout = ctx.tools["nelix_dialog"]["handler"]({"session_id": "s1"})
        assert isinstance(json.loads(dout), dict) and "error" not in json.loads(dout)
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
        for tname in ("nelix_start", "nelix_status", "nelix_respond", "nelix_stop", "nelix_dialog",
                      "nelix_screen", "nelix_models"):
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


def test_nelix_start_schema_has_optional_model(monkeypatch, tmp_path):
    # nelix-9k0: the tool schema gains an optional `model` (NOT in required); its description says it
    # accepts a tier alias or a full model id, omit for the executor default.
    nelix = _load_with_fake(monkeypatch, tmp_path)
    ctx = FakeCtx()
    try:
        nelix.register(ctx)
        params = ctx.tools["nelix_start"]["schema"]["parameters"]
        assert "model" in params["properties"]
        assert params["properties"]["model"]["type"] == "string"
        assert params["required"] == ["executor", "task"]          # model stays optional
        assert params["properties"]["model"].get("description")     # documented for the LLM
    finally:
        nelix.supervisor.teardown()


def test_nelix_start_threads_model_to_rpcclient(monkeypatch, tmp_path):
    # The handler passes args.get("model") into RpcClient.start; omitted -> None (no wire change).
    nelix = _load_with_fake(monkeypatch, tmp_path)
    ctx = FakeCtx()
    captured = {}

    class FakeRpc:
        def __init__(self, *a, **k): pass
        def start(self, executor, task, cwd, model=None):
            captured["model"] = model
            return {"session_id": "s1", "next_after_seq": 0}
    monkeypatch.setattr(nelix, "RpcClient", FakeRpc)
    try:
        nelix.register(ctx)
        ctx.tools["nelix_start"]["handler"]({"executor": "opencode", "task": "go", "model": "haiku"})
        assert captured["model"] == "haiku"
        captured.clear()
        ctx.tools["nelix_start"]["handler"]({"executor": "opencode", "task": "go"})
        assert captured["model"] is None
    finally:
        nelix.supervisor.teardown()


def test_nelix_models_schema_and_relays_body(monkeypatch, tmp_path):
    # nelix-g9k: read-only tool; schema {executor} required. The handler runs config_error_for,
    # ensures the daemon, calls RpcClient.models, and relays the body (drops the status).
    nelix = _load_with_fake(monkeypatch, tmp_path)
    ctx = FakeCtx()
    try:
        nelix.register(ctx)
        params = ctx.tools["nelix_models"]["schema"]["parameters"]
        assert params["required"] == ["executor"]
        assert set(params["properties"]) == {"executor"}
        assert ctx.tools["nelix_models"]["schema"]["description"]         # documented for the LLM
        # Avoid spinning the fake daemon: stub ensure_running + RpcClient.
        monkeypatch.setattr(nelix.supervisor, "ensure_running",
                            lambda: Transport.tcp("127.0.0.1", 9999, "t"))
        captured = {}

        class FakeRpc:
            def __init__(self, *a, **k): pass
            def models(self, executor):
                captured["executor"] = executor
                return 200, {"output": "model-x\nmodel-y (Display)", "truncated": False}
        monkeypatch.setattr(nelix, "RpcClient", FakeRpc)
        out = ctx.tools["nelix_models"]["handler"]({"executor": "opencode"})
        assert captured["executor"] == "opencode"
        assert json.loads(out) == {"output": "model-x\nmodel-y (Display)", "truncated": False}
    finally:
        nelix.supervisor.teardown()


def test_nelix_models_relays_config_error_without_touching_daemon(monkeypatch, tmp_path):
    # A broken/disabled executor config is relayed as a message; the daemon is NEVER spun up.
    nelix = _load_with_fake(monkeypatch, tmp_path)
    ctx = FakeCtx()
    try:
        nelix.register(ctx)
        monkeypatch.setattr(nelix.registry, "config_error_for", lambda v, e: {"error": "boom-config"})
        called = {"ensure": False}
        monkeypatch.setattr(nelix.supervisor, "ensure_running",
                            lambda: called.__setitem__("ensure", True) or None)
        out = ctx.tools["nelix_models"]["handler"]({"executor": "opencode"})
        assert json.loads(out)["error"] == "boom-config"
        assert called["ensure"] is False                                  # config-first: no daemon
    finally:
        nelix.supervisor.teardown()


def test_nelix_models_relays_error_body_on_failure_status(monkeypatch, tmp_path):
    # On a 502/404/400 the handler relays the clean {error} body unchanged (status dropped).
    nelix = _load_with_fake(monkeypatch, tmp_path)
    ctx = FakeCtx()
    try:
        nelix.register(ctx)
        monkeypatch.setattr(nelix.supervisor, "ensure_running",
                            lambda: Transport.tcp("127.0.0.1", 9999, "t"))

        class FakeRpc:
            def __init__(self, *a, **k): pass
            def models(self, executor):
                return 502, {"error": {"executor": executor, "reason": "timeout"}}
        monkeypatch.setattr(nelix, "RpcClient", FakeRpc)
        out = ctx.tools["nelix_models"]["handler"]({"executor": "opencode"})
        assert json.loads(out)["error"] == {"executor": "opencode", "reason": "timeout"}
    finally:
        nelix.supervisor.teardown()


def test_nelix_respond_binds_to_session_without_event_id(monkeypatch, tmp_path):
    # The MCP tool takes no opaque event_id. decision_id stays OPTIONAL in the schema (the tool has
    # two modes — answer-a-question vs idle-follow-up); the real guard is in the daemon by state.
    # To answer a pending question the caller passes decision_id and the plugin forwards it verbatim
    # to RpcClient.respond; on success it arms the next doorbell.
    nelix = _load_with_fake(monkeypatch, tmp_path)
    ctx = FakeCtx()
    try:
        nelix.register(ctx)
        params = ctx.tools["nelix_respond"]["schema"]["parameters"]
        assert params["required"] == ["session_id", "answer"]      # event_id is gone
        assert "event_id" not in params["properties"]
        assert "decision_id" in params["properties"]               # optional in schema (two modes)
        monkeypatch.setattr(nelix.supervisor, "endpoint", lambda: Transport.tcp("127.0.0.1", 9999, "t"))
        captured = {}

        class FakeRpc:
            def __init__(self, *a, **k): pass
            def respond(self, session_id, answer, decision_id=None):
                captured.update(session_id=session_id, answer=answer, decision_id=decision_id)
                return True, {"status": "resumed", "next_after_seq": 5, "decision_id": "dec-1"}
        monkeypatch.setattr(nelix, "RpcClient", FakeRpc)
        out = ctx.tools["nelix_respond"]["handler"](
            {"session_id": "s1", "answer": "1", "decision_id": "dec-1"})
        assert json.loads(out)["status"] == "resumed"
        assert captured == {"session_id": "s1", "answer": "1", "decision_id": "dec-1"}
        assert ctx.dispatched and ctx.dispatched[-1][0] == "terminal"   # next doorbell armed
    finally:
        nelix.supervisor.teardown()


def test_nelix_respond_description_names_idle_deliver_now_outcome(monkeypatch, tmp_path):
    # nelix-wp9: answering an async_question.id has THREE outcomes, not two. Besides queued (busy)
    # and not_delivered (exited), an IDLE agent gets the answer delivered IMMEDIATELY as a fresh
    # turn — the daemon returns the normal resumed envelope (status:"resumed", next_action:"end_turn"),
    # NOT status:"queued". The description previously mentioned `resumed` only in the negative
    # ("not_delivered instead of resumed"), so an orchestrator had nothing to key on and narrated an
    # immediate delivery as "queued". The contract must name status:"resumed" for this path.
    nelix = _load_with_fake(monkeypatch, tmp_path)
    ctx = FakeCtx()
    try:
        nelix.register(ctx)
        desc = ctx.tools["nelix_respond"]["schema"]["description"]
        assert 'status:"queued"' in desc          # busy (already documented)
        assert 'status:"not_delivered"' in desc    # exited (already documented)
        assert 'status:"resumed"' in desc          # idle -> deliver_now (nelix-wp9: newly named)
        assert "immediately" in desc.lower()       # delivered now, not queued for later
    finally:
        nelix.supervisor.teardown()


def test_session_finalize_hook_calls_teardown(monkeypatch, tmp_path):
    nelix = _load_with_fake(monkeypatch, tmp_path)
    called = {}
    monkeypatch.setattr(nelix.supervisor, "teardown", lambda *a, **k: called.setdefault("t", True))
    ctx = FakeCtx()
    nelix.register(ctx)
    # teardown must be on on_session_finalize (true teardown), NOT on_session_end (per-turn)
    assert "on_session_end" not in ctx.hooks
    ctx.hooks["on_session_finalize"]()
    assert called.get("t") is True


def test_nelix_start_no_waiter_on_failed_start(monkeypatch, tmp_path):
    # A failed start (e.g. bad cwd) returns an error body with no session_id; we must NOT arm a
    # waiter — an unscoped orphan waiter would later wake on an unrelated session's event.
    nelix = _load_with_fake(monkeypatch, tmp_path)
    ctx = FakeCtx()
    try:
        nelix.register(ctx)

        class FailStart:
            def __init__(self, *a, **k): pass
            def start(self, *a, **k):
                return {"error": "cwd does not exist or is not a directory: '/nope'"}
        monkeypatch.setattr(nelix, "RpcClient", FailStart)
        out = ctx.tools["nelix_start"]["handler"]({"executor": "opencode", "task": "go", "cwd": "/nope"})
        assert "error" in json.loads(out)
        assert ctx.dispatched == []                  # no orphan waiter armed
    finally:
        nelix.supervisor.teardown()


def test_nelix_start_logs_metadata_not_task_text(monkeypatch, tmp_path, caplog):
    nelix = _load_with_fake(monkeypatch, tmp_path)
    ctx = FakeCtx()
    try:
        nelix.register(ctx)
        SECRET_TASK = "delete prod database please"
        with caplog.at_level("INFO", logger="nelix"):
            ctx.tools["nelix_start"]["handler"]({"executor": "opencode", "task": SECRET_TASK})
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "opencode" in msgs            # executor metadata logged
        assert SECRET_TASK not in msgs        # task body must NOT be logged
    finally:
        nelix.supervisor.teardown()


def test_nelix_start_disabled_executor_returns_config_error(monkeypatch, tmp_path):
    # Good 'opencode' + a 'broken' executor missing its driver: starting 'broken' must
    # return a structured config error WITHOUT contacting the daemon.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = tmp_path / "workspace" / "nelix" / "nelix.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[executors.opencode]\ncommand="opencode"\ndriver="claude"\n'
                   '[executors.broken]\ncommand="x"\n')
    nelix = load_plugin()
    monkeypatch.setattr(nelix, "resolve_launcher", lambda *a, **k: "local")
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("daemon must not be contacted"))
    monkeypatch.setattr(nelix.supervisor, "ensure_running", boom)
    ctx = FakeCtx()
    nelix.register(ctx)
    out = json.loads(ctx.tools["nelix_start"]["handler"]({"executor": "broken", "task": "go"}))
    assert "broken" in out["error"] and "config" in out["error"].lower()
    assert out["config_errors"] and out["config_errors"][0]["name"] == "broken"


def test_nelix_start_parse_error_returns_config_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = tmp_path / "workspace" / "nelix" / "nelix.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text('[oops')                                   # whole-file parse error
    nelix = load_plugin()
    monkeypatch.setattr(nelix, "resolve_launcher", lambda *a, **k: "local")
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("daemon must not be contacted"))
    monkeypatch.setattr(nelix.supervisor, "ensure_running", boom)
    ctx = FakeCtx()
    nelix.register(ctx)
    out = json.loads(ctx.tools["nelix_start"]["handler"]({"executor": "anything", "task": "go"}))
    assert "config" in out["error"].lower() and out["config_errors"]
