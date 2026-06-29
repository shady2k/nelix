import threading
from nelix_cursor import WakeRegistry


def test_on_start_sets_value_and_claims_one_waiter():
    r = WakeRegistry()
    r.on_start("s-a", base_seq=3, daemon_id=1)
    assert r.value("s-a") == 3
    assert r.claim_arm("s-a") == 3        # first claim arms at base
    assert r.claim_arm("s-a") is None     # already armed for value 3 -> no second waiter


def test_after_seq_zero_is_not_skipped():
    r = WakeRegistry()
    r.on_start("s-a", base_seq=0, daemon_id=1)
    assert r.claim_arm("s-a") == 0        # 0 is a valid after_seq, must arm (not falsy-skipped)


def test_status_advance_rearms():
    r = WakeRegistry()
    r.on_start("s-a", 0, daemon_id=1)
    assert r.claim_arm("s-a") == 0
    r.on_status("s-a", 7)
    assert r.claim_arm("s-a") == 7        # cursor advanced -> re-arm
    assert r.claim_arm("s-a") is None


def test_respond_advances_per_session_cursor():
    r = WakeRegistry()
    r.on_start("s-a", 0, daemon_id=1)
    r.claim_arm("s-a")
    r.on_respond("s-a", 5)                # per-session advance is correct (no cross-session skip)
    assert r.value("s-a") == 5
    assert r.claim_arm("s-a") == 5


def test_sessions_are_independent():
    r = WakeRegistry()
    r.on_start("s-a", 0, daemon_id=1)
    r.on_start("s-b", 0, daemon_id=1)
    assert r.claim_arm("s-a") == 0 and r.claim_arm("s-b") == 0
    r.on_status("s-a", 9)                 # A advances; B untouched
    assert r.claim_arm("s-a") == 9
    assert r.claim_arm("s-b") is None     # B still armed at 0 — answering/advancing A never skips B


def test_drop_removes_session():
    r = WakeRegistry()
    r.on_start("s-a", 0, daemon_id=1)
    r.claim_arm("s-a")
    r.drop("s-a")
    assert r.value("s-a") is None
    assert "s-a" not in r.active_sids()
    assert r.claim_arm("s-a") is None     # dropped -> nothing to arm


def test_new_daemon_clears_registry():
    r = WakeRegistry()
    r.on_start("s-a", 5, daemon_id=111)
    r.claim_arm("s-a")
    r.on_start("s-b", 0, daemon_id=222)   # NEW daemon (pid change): old sessions are gone
    assert r.value("s-a") is None         # cleared
    assert r.value("s-b") == 0
    assert r.claim_arm("s-b") == 0


def test_concurrent_claim_arms_exactly_once():
    r = WakeRegistry()
    r.on_start("s-a", 0, daemon_id=1)
    results = []
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()
        results.append(r.claim_arm("s-a"))

    ts = [threading.Thread(target=worker) for _ in range(20)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert sum(1 for x in results if x is not None) == 1   # exactly one waiter claimed


# ---------------------------------------------------------------------------
# Plugin-integration: per-session arm / re-arm / drop across the tool handlers
# ---------------------------------------------------------------------------
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from plugin_loader import load_plugin
from test_plugin_register import FakeCtx


def _terminal_cmds(ctx):
    return [args["command"] for name, args in ctx.dispatched if name == "terminal"]


def _setup(monkeypatch, tmp_path, client_cls):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    nelix = load_plugin()
    monkeypatch.setattr(nelix, "RpcClient", client_cls)
    monkeypatch.setattr(nelix.supervisor, "ensure_running", lambda: object())
    monkeypatch.setattr(nelix.supervisor, "endpoint", lambda: object())
    monkeypatch.setattr(nelix.supervisor, "state_file", lambda: str(tmp_path / "st.json"))
    monkeypatch.setattr(nelix, "resolve_launcher", lambda *a, **k: None)
    monkeypatch.setattr(nelix.registry, "config_error_for", lambda *a, **k: None)
    monkeypatch.setattr(nelix.registry, "validate", lambda: {})
    monkeypatch.setattr(nelix.registry, "seed_if_absent", lambda: None)
    # stable daemon id so on_start does not reset the registry mid-test
    (tmp_path / "st.json").write_text('{"pid": 4242}')
    ctx = FakeCtx()
    nelix.register(ctx)
    return nelix, ctx


def test_start_arms_per_session_waiter(monkeypatch, tmp_path):
    class C:
        def __init__(self, t): pass
        def start(self, *a): return {"session_id": "s-1", "next_after_seq": 0}
    _, ctx = _setup(monkeypatch, tmp_path, C)
    ctx.tools["nelix_start"]["handler"]({"executor": "claude", "task": "t", "cwd": str(tmp_path)})
    cmds = _terminal_cmds(ctx)
    assert len(cmds) == 1
    assert "--session-id s-1" in cmds[-1] and "--after 0" in cmds[-1]


def test_board_read_rearms_after_wake_second_event_wakes_again(monkeypatch, tmp_path):
    """THE primary bug anchor: a wake -> board read re-arms -> a second event delivers a
    second wake. Modelled as: start (arm@0), board shows seq=4 -> re-arm@4."""
    class C:
        def __init__(self, t): pass
        def start(self, *a): return {"session_id": "s-1", "next_after_seq": 0}
        def status(self, sid=None):
            # all-sessions board read after the first wake; session advanced to seq 4
            return {"sessions": {"s-1": {"session_id": "s-1", "state": "waiting_for_user",
                                         "seq": 4, "decision": {"seq": 4}}},
                    "recent_terminal": {}, "cursor": 4}
    _, ctx = _setup(monkeypatch, tmp_path, C)
    ctx.tools["nelix_start"]["handler"]({"executor": "claude", "task": "t", "cwd": str(tmp_path)})
    ctx.tools["nelix_status"]["handler"]({})           # board read
    cmds = _terminal_cmds(ctx)
    assert len(cmds) == 2                               # re-armed -> a second waiter exists
    assert "--session-id s-1" in cmds[-1] and "--after 4" in cmds[-1]


def test_board_read_drops_terminal_session(monkeypatch, tmp_path):
    class C:
        def __init__(self, t): pass
        def start(self, *a): return {"session_id": "s-1", "next_after_seq": 0}
        def status(self, sid=None):
            # s-1 finished: absent from live sessions, present in recent_terminal
            return {"sessions": {}, "recent_terminal": {"s-1": {"terminal": True}}, "cursor": 9}
    nelix, ctx = _setup(monkeypatch, tmp_path, C)
    ctx.tools["nelix_start"]["handler"]({"executor": "claude", "task": "t", "cwd": str(tmp_path)})
    n_before = len(_terminal_cmds(ctx))
    ctx.tools["nelix_status"]["handler"]({})            # board read sees it terminal
    assert len(_terminal_cmds(ctx)) == n_before         # NO new waiter for a terminal session


def test_per_session_status_unknown_drops(monkeypatch, tmp_path):
    class C:
        def __init__(self, t): pass
        def start(self, *a): return {"session_id": "s-1", "next_after_seq": 0}
        def status(self, sid=None):
            return {"error": "unknown session"}         # per-session read of a gone session
    nelix, ctx = _setup(monkeypatch, tmp_path, C)
    ctx.tools["nelix_start"]["handler"]({"executor": "claude", "task": "t", "cwd": str(tmp_path)})
    n_before = len(_terminal_cmds(ctx))
    ctx.tools["nelix_status"]["handler"]({"session_id": "s-1"})
    assert len(_terminal_cmds(ctx)) == n_before         # dropped, no re-arm


def test_respond_rearms_without_prior_status(monkeypatch, tmp_path):
    class C:
        def __init__(self, t): pass
        def start(self, *a): return {"session_id": "s-1", "next_after_seq": 0}
        def respond(self, *a, **k): return True, {"next_after_seq": 6}
    _, ctx = _setup(monkeypatch, tmp_path, C)
    ctx.tools["nelix_start"]["handler"]({"executor": "claude", "task": "t", "cwd": str(tmp_path)})
    ctx.tools["nelix_respond"]["handler"]({"session_id": "s-1", "answer": "1"})
    cmds = _terminal_cmds(ctx)
    assert "--session-id s-1" in cmds[-1] and "--after 6" in cmds[-1]   # armed past answered seq
