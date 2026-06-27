import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from plugin_loader import load_plugin
from test_plugin_register import FakeCtx


def _load(monkeypatch, tmp_path, restart_result):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    nelix = load_plugin()

    class _Client:
        def __init__(self, t): pass
        def status(self, sid=None): return {"sessions": {}, "cursor": 3}
        def restart(self, session_id, force=False): return restart_result
    monkeypatch.setattr(nelix, "RpcClient", _Client)
    monkeypatch.setattr(nelix.supervisor, "endpoint", lambda: object())
    monkeypatch.setattr(nelix.supervisor, "state_file", lambda: str(tmp_path / "st.json"))
    monkeypatch.setattr(nelix.registry, "config_error_for", lambda *a, **k: None)
    monkeypatch.setattr(nelix.registry, "validate", lambda: {})
    monkeypatch.setattr(nelix.registry, "seed_if_absent", lambda: None)
    ctx = FakeCtx()
    nelix.register(ctx)
    return nelix, ctx


def _n_terminal(ctx):
    return sum(1 for n, _ in ctx.dispatched if n == "terminal")


def test_restart_tool_success_arms_waiter(monkeypatch, tmp_path):
    nelix, ctx = _load(monkeypatch, tmp_path,
        (True, {"status": "restarted", "session_id": "s-2", "next_after_seq": 0}))
    out = json.loads(ctx.tools["nelix_restart"]["handler"]({"session_id": "s-1"}))
    assert out["status"] == "restarted" and out["session_id"] == "s-2"
    assert _n_terminal(ctx) == 1                    # armed one global waiter after restart


def test_restart_tool_budget_exhausted_no_arm(monkeypatch, tmp_path):
    nelix, ctx = _load(monkeypatch, tmp_path,
        (False, {"error": "restart_budget_exhausted", "restart_count": 3, "max_restarts": 3}))
    out = json.loads(ctx.tools["nelix_restart"]["handler"]({"session_id": "s-1"}))
    assert out["error"] == "restart_budget_exhausted"
    assert _n_terminal(ctx) == 0                    # failed restart does not arm a waiter
