import json
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
        def status(self, sid=None, include_progress=False):
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
        def status(self, sid=None, include_progress=False):
            # s-1 finished: absent from live sessions, present in recent_terminal
            return {"sessions": {}, "recent_terminal": {"s-1": {"terminal": True}}, "cursor": 9}
    nelix, ctx = _setup(monkeypatch, tmp_path, C)
    ctx.tools["nelix_start"]["handler"]({"executor": "claude", "task": "t", "cwd": str(tmp_path)})
    n_before = len(_terminal_cmds(ctx))
    ctx.tools["nelix_status"]["handler"]({})            # board read sees it terminal
    assert len(_terminal_cmds(ctx)) == n_before         # NO new waiter for a terminal session


def test_board_read_drops_live_listed_terminal_session(monkeypatch, tmp_path):
    """Publish-to-free window: a finished session is STILL listed in `sessions` (not yet moved to
    recent_terminal) but carries a terminal_kind. A clean exit reports state='exited' (NOT 'done'),
    so detection must key on terminal_kind, not a state allowlist — else a waiter is re-armed on a
    dead session that emits no further events and is stranded."""
    class C:
        def __init__(self, t): pass
        def start(self, *a): return {"session_id": "s-1", "next_after_seq": 0}
        def status(self, sid=None, include_progress=False):
            return {"sessions": {"s-1": {"session_id": "s-1", "state": "exited",
                                         "terminal_kind": "done", "seq": 9}},
                    "recent_terminal": {}, "cursor": 9}
    nelix, ctx = _setup(monkeypatch, tmp_path, C)
    ctx.tools["nelix_start"]["handler"]({"executor": "claude", "task": "t", "cwd": str(tmp_path)})
    n_before = len(_terminal_cmds(ctx))
    ctx.tools["nelix_status"]["handler"]({})            # board still lists it, but it's terminal
    assert len(_terminal_cmds(ctx)) == n_before         # NO new waiter -> not stranded


def test_per_session_status_live_terminal_kind_drops(monkeypatch, tmp_path):
    """Per-session read in the same window: a live snapshot with terminal_kind='delivery_failed'
    must drop, not re-arm."""
    class C:
        def __init__(self, t): pass
        def start(self, *a): return {"session_id": "s-1", "next_after_seq": 0}
        def status(self, sid=None, include_progress=False):
            return {"session_id": "s-1", "state": "working",
                    "terminal_kind": "delivery_failed", "cursor": 7}
    nelix, ctx = _setup(monkeypatch, tmp_path, C)
    ctx.tools["nelix_start"]["handler"]({"executor": "claude", "task": "t", "cwd": str(tmp_path)})
    n_before = len(_terminal_cmds(ctx))
    ctx.tools["nelix_status"]["handler"]({"session_id": "s-1"})
    assert len(_terminal_cmds(ctx)) == n_before         # dropped, no re-arm


def test_per_session_status_unknown_drops(monkeypatch, tmp_path):
    class C:
        def __init__(self, t): pass
        def start(self, *a): return {"session_id": "s-1", "next_after_seq": 0}
        def status(self, sid=None, include_progress=False):
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
        def respond(self, *a, **k): return True, {"status": "resumed", "next_after_seq": 6}
    _, ctx = _setup(monkeypatch, tmp_path, C)
    ctx.tools["nelix_start"]["handler"]({"executor": "claude", "task": "t", "cwd": str(tmp_path)})
    ctx.tools["nelix_respond"]["handler"]({"session_id": "s-1", "answer": "1"})
    cmds = _terminal_cmds(ctx)
    assert "--session-id s-1" in cmds[-1] and "--after 6" in cmds[-1]   # armed past answered seq


def test_respond_failed_arms_no_waiter_and_keeps_recover(monkeypatch, tmp_path):
    # nelix-sud: an unconfirmed submit (HTTP 503 -> ok=False) must NOT arm a waiter (the turn did
    # not resume) and must keep the daemon-owned next_action='recover' so the orchestrator recovers.
    class C:
        def __init__(self, t): pass
        def start(self, *a): return {"session_id": "s-1", "next_after_seq": 0}
        def respond(self, *a, **k):
            return False, {"operation": "respond", "status": "respond_failed", "session_id": "s-1",
                           "snapshot": {"session_id": "s-1", "control_state": "busy", "pending": False},
                           "answered_decision_id": "dec-1", "next_action": "recover",
                           "error": "submit_unconfirmed"}
    _, ctx = _setup(monkeypatch, tmp_path, C)
    ctx.tools["nelix_start"]["handler"]({"executor": "claude", "task": "t", "cwd": str(tmp_path)})
    cmds_before = len(_terminal_cmds(ctx))
    body = json.loads(ctx.tools["nelix_respond"]["handler"]({"session_id": "s-1", "answer": "go now"}))
    assert body["status"] == "respond_failed" and body["next_action"] == "recover"
    assert body["waiter"]["armed"] is False               # the turn did not resume -> no wake armed
    assert len(_terminal_cmds(ctx)) == cmds_before        # no new waiter dispatched


def test_respond_not_delivered_surfaces_cleanly_no_waiter_arm(monkeypatch, tmp_path):
    # Task 8: an async-question answer that arrived after the executor already finished (Task 6,
    # reason='executor_finished') must reach Hermes as a clean, readable outcome -- not silently
    # swallowed, and armed=False (there is nothing left to wake for on this session).
    class C:
        def __init__(self, t): pass
        def start(self, *a): return {"session_id": "s-1", "next_after_seq": 0}
        def respond(self, *a, **k):
            return True, {"operation": "respond", "status": "not_delivered", "session_id": "s-1",
                          "reason": "executor_finished", "next_action": "refresh_status"}
    _, ctx = _setup(monkeypatch, tmp_path, C)
    ctx.tools["nelix_start"]["handler"]({"executor": "claude", "task": "t", "cwd": str(tmp_path)})
    cmds_before = len(_terminal_cmds(ctx))
    body = json.loads(ctx.tools["nelix_respond"]["handler"](
        {"session_id": "s-1", "answer": "use a", "decision_id": "q_1"}))
    assert body["status"] == "not_delivered" and body["reason"] == "executor_finished"
    assert body["next_action"] == "refresh_status"
    assert body["waiter"]["armed"] is False
    assert len(_terminal_cmds(ctx)) == cmds_before        # no new waiter dispatched


class _EnvelopeClient:
    def __init__(self, t): pass
    def start(self, *a):
        return {"operation": "start", "status": "started", "session_id": "s-1",
                "snapshot": {"session_id": "s-1", "control_state": "busy"},
                "next_after_seq": 0, "next_action": "end_turn"}
    def stop(self, sid):
        return {"operation": "stop", "status": "stopped", "session_id": sid,
                "snapshot": {"session_id": sid, "control_state": "terminal",
                             "terminal_kind": "stopped"}, "next_action": "report"}
    def status(self, sid=None, include_progress=False): return {"sessions": {}}


def test_start_envelope_adds_armed_waiter(monkeypatch, tmp_path):
    _, ctx = _setup(monkeypatch, tmp_path, _EnvelopeClient)
    body = json.loads(ctx.tools["nelix_start"]["handler"](
        {"executor": "claude", "task": "go", "cwd": str(tmp_path)}))
    assert body["operation"] == "start" and body["next_action"] == "end_turn"
    assert body["waiter"] == {"armed": True, "after_seq": 0}


def test_stop_envelope_waiter_not_armed(monkeypatch, tmp_path):
    _, ctx = _setup(monkeypatch, tmp_path, _EnvelopeClient)
    body = json.loads(ctx.tools["nelix_stop"]["handler"]({"session_id": "s-1"}))
    assert body["waiter"]["armed"] is False and body["status"] == "stopped"


# ---------------------------------------------------------------------------
# Fix 3 — _with_waiter downgrade: arm skipped (claim_arm dedup) on success body
# ---------------------------------------------------------------------------

def test_with_waiter_downgrade_when_arm_skipped(monkeypatch, tmp_path):
    """Legitimate downgrade: second nelix_start on the same session hits claim_arm dedup
    (waiter already armed at cursor 0); armed_after=None with next_action='end_turn'
    must be downgraded to 'refresh_status', waiter.armed=False."""
    class C:
        def __init__(self, t): pass
        def start(self, *a):
            return {"operation": "start", "status": "started", "session_id": "s-1",
                    "snapshot": {"session_id": "s-1", "control_state": "busy"},
                    "next_after_seq": 0, "next_action": "end_turn"}
    _, ctx = _setup(monkeypatch, tmp_path, C)
    start = ctx.tools["nelix_start"]["handler"]
    start({"executor": "claude", "task": "t1", "cwd": str(tmp_path)})  # arms at seq 0
    body = json.loads(start({"executor": "claude", "task": "t2", "cwd": str(tmp_path)}))
    # second call: claim_arm returns None (already armed at 0) -> downgrade fires
    assert body["next_action"] == "refresh_status"   # downgraded from end_turn
    assert body["waiter"]["armed"] is False


# ---------------------------------------------------------------------------
# Fix 1 plugin — stop_requested: refresh_status + armed wake; stopped: report + no wake
# ---------------------------------------------------------------------------

class _StopRequestedClient:
    """Client that returns stop_requested from stop(); start arms the session registry."""
    def __init__(self, t): pass
    def start(self, *a):
        return {"operation": "start", "status": "started", "session_id": "s-1",
                "snapshot": {"session_id": "s-1", "control_state": "busy"},
                "next_after_seq": 0, "next_action": "end_turn"}
    def stop(self, sid):
        return {"operation": "stop", "status": "stop_requested", "session_id": sid,
                "snapshot": {"session_id": sid, "control_state": "stopping", "pending": False},
                "next_action": "refresh_status"}
    def status(self, sid=None, include_progress=False): return {"sessions": {}}


def test_stop_requested_refresh_status_and_armed_wake(monkeypatch, tmp_path):
    """stop_requested: daemon-owned next_action=refresh_status; plugin reports waiter.armed=True
    because the session is tracked (existing or freshly armed waiter will fire on terminal)."""
    _, ctx = _setup(monkeypatch, tmp_path, _StopRequestedClient)
    ctx.tools["nelix_start"]["handler"]({"executor": "claude", "task": "t", "cwd": str(tmp_path)})
    body = json.loads(ctx.tools["nelix_stop"]["handler"]({"session_id": "s-1"}))
    assert body["next_action"] == "refresh_status"
    assert body["waiter"]["armed"] is True


def test_stop_confirmed_report_and_no_wake(monkeypatch, tmp_path):
    """stopped: next_action=report, waiter.armed=False (session dropped)."""
    _, ctx = _setup(monkeypatch, tmp_path, _EnvelopeClient)
    ctx.tools["nelix_start"]["handler"]({"executor": "claude", "task": "t", "cwd": str(tmp_path)})
    body = json.loads(ctx.tools["nelix_stop"]["handler"]({"session_id": "s-1"}))
    assert body["next_action"] == "report"
    assert body["waiter"]["armed"] is False
