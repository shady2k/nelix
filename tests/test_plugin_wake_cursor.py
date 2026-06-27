from nelix_cursor import CursorState   # new module created in Step 3


def test_first_start_sets_cursor_to_base_seq():
    c = CursorState()
    c.on_start(base_seq=0)
    assert c.value == 0


def test_burst_start_does_not_advance_cursor():
    c = CursorState()
    c.on_start(base_seq=0)        # start A
    c.on_start(base_seq=1)        # start B emitted after A -> must NOT push cursor to 1
    assert c.value == 0           # still armed at the lowest unobserved point


def test_status_advances_cursor():
    c = CursorState()
    c.on_start(base_seq=0)
    c.on_status(cursor=7)
    assert c.value == 7


def test_respond_never_changes_cursor():
    c = CursorState()
    c.on_start(base_seq=0)
    c.on_status(cursor=7)
    c.on_respond(next_after_seq=99)   # per-session seq must be ignored for the global cursor
    assert c.value == 7


def test_new_daemon_resets_cursor_down():
    c = CursorState()
    c.on_start(base_seq=0, daemon_id=111)
    c.on_status(cursor=50)            # long-lived daemon advanced
    c.on_start(base_seq=0, daemon_id=222)  # NEW daemon (pid changed): reset down, don't wait forever
    assert c.value == 0


def test_should_arm_collapses_burst_to_one():
    c = CursorState()
    c.on_start(base_seq=0, daemon_id=111)
    assert c.should_arm() is True     # nothing armed yet
    c.mark_armed()
    c.on_start(base_seq=1, daemon_id=111)  # burst, same daemon: value stays 0
    assert c.should_arm() is False    # a waiter is already out for value 0 -> skip
    c.on_status(cursor=7)             # real wake handled -> cursor advanced
    assert c.should_arm() is True     # re-arm at the new value


def test_new_daemon_rearms_even_when_both_cursors_zero():
    # The cursor=0 collision: old daemon armed at 0 and died before any status advanced; a fresh
    # daemon also starts at base_seq 0. A seq-only rule would leave should_arm() False -> no waiter.
    c = CursorState()
    c.on_start(base_seq=0, daemon_id=111)
    c.mark_armed()                    # old daemon's waiter armed at 0
    assert c.should_arm() is False
    c.on_start(base_seq=0, daemon_id=222)  # NEW daemon, same seq 0 -> must re-arm
    assert c.should_arm() is True and c.value == 0


# ---------------------------------------------------------------------------
# Plugin-integration: unscoped arming + cursor source
# ---------------------------------------------------------------------------
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from plugin_loader import load_plugin
from test_plugin_register import FakeCtx


def _last_terminal_cmd(ctx):
    for name, args in reversed(ctx.dispatched):
        if name == "terminal":
            return args["command"]
    return None


def test_plugin_arms_one_global_waiter_from_cursor(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    nelix = load_plugin()

    class _Client:
        def __init__(self, t): pass
        def start(self, *a): return {"session_id": "s-1", "next_after_seq": 0}
        def status(self, sid=None): return {"sessions": {}, "cursor": 5}
        def respond(self, *a, **k): return True, {"next_after_seq": 99}
    monkeypatch.setattr(nelix, "RpcClient", _Client)
    monkeypatch.setattr(nelix.supervisor, "ensure_running", lambda: object())
    monkeypatch.setattr(nelix.supervisor, "endpoint", lambda: object())
    monkeypatch.setattr(nelix.supervisor, "state_file", lambda: str(tmp_path / "st.json"))
    monkeypatch.setattr(nelix, "resolve_launcher", lambda *a, **k: None)
    monkeypatch.setattr(nelix.registry, "config_error_for", lambda *a, **k: None)
    monkeypatch.setattr(nelix.registry, "validate", lambda: {})
    monkeypatch.setattr(nelix.registry, "seed_if_absent", lambda: None)

    ctx = FakeCtx()
    nelix.register(ctx)
    start = ctx.tools["nelix_start"]["handler"]
    status = ctx.tools["nelix_status"]["handler"]
    respond = ctx.tools["nelix_respond"]["handler"]

    start({"executor": "claude", "task": "t", "cwd": str(tmp_path)})
    cmd = _last_terminal_cmd(ctx)
    assert "--after 0" in cmd and "--session-id" not in cmd      # global waiter at cursor baseline 0

    start({"executor": "claude", "task": "t2", "cwd": str(tmp_path)})   # burst: cursor still 0
    n_terminal = sum(1 for n, _ in ctx.dispatched if n == "terminal")
    assert n_terminal == 1                                        # exactly one waiter for the burst

    status({})                                                   # advances cursor to 5
    respond({"session_id": "s-1", "answer": "1"})
    cmd = _last_terminal_cmd(ctx)
    assert "--after 5" in cmd and "--session-id" not in cmd       # armed from cursor (5), NOT respond's 99
