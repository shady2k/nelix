"""Task 9: manager active-vs-live slot accounting + idle_retained_limit.

An `idle` session (turn complete, alive, awaiting a follow-up) is RETAINED but does not occupy an
active concurrency slot: it frees a slot for a new start, yet is bounded by a separate
`idle_retained_limit` so completed-but-unclosed sessions cannot accumulate without bound. Every other
live control_state (busy / awaiting_user / intervention_required / starting) still holds its slot —
the rule is exclude-idle, not a positive busy-only allowlist.
"""
import pytest
from daemon.events import EventQueue
from daemon.manager import SessionManager
from daemon.config import ExecutorSpec, load_idle_retained_limit
from conftest import OWNER, reserve_start


class FakeSession:
    """Manager-facing session double with a mutable `control_state` so a test can drive it from
    active (busy/awaiting_user) to `idle`, and record what was typed via send_turn/respond."""
    def __init__(self, sid, executor, spec):
        self.on_terminal = None
        self.reaper_ctx = None
        self.lineage_id = None
        self.restarted_from = None
        self.restart_count = 0
        self._id = sid
        self.executor = executor
        self.control_state = "busy"
        self.started = False
        self.sent = []          # texts routed through send_turn
        self.responded = []     # answers routed through respond

    def start(self, task, cwd): self.started = True
    def snapshot(self): return {"session_id": self._id, "executor": self.executor,
                                 "control_state": self.control_state,
                                 "task_delivery": "pending", "pending": False}
    def send_turn(self, text):
        self.sent.append(text)
        self.control_state = "busy"
        from daemon.session import RespondOutcome
        return RespondOutcome("resumed")
    def respond(self, answer, decision_id=None):
        self.responded.append(answer)
        from daemon.session import RespondOutcome
        return RespondOutcome("resumed")
    def stop(self): pass


def _manager(store_and_ledger, tmp_path, limit=2, idle_retained_limit=None):
    store, ledger = store_and_ledger
    specs = {"claude": ExecutorSpec(command="claude", args=[], env={}, driver="claude")}
    events = EventQueue()
    created = []

    def factory(sid, ex, spec, ev):
        s = FakeSession(sid, ex, spec)
        created.append(s)
        return s

    mgr = SessionManager(specs, events, store, concurrency_limit=limit,
                         idle_retained_limit=idle_retained_limit,
                         session_factory=factory,
                         session_retain=0, session_max_age_days=0)
    return mgr, created, ledger


# ---- active-vs-live counting ----

def test_idle_session_frees_an_active_slot(tmp_path, store_and_ledger):
    cwd = str(tmp_path)
    mgr, created, ledger = _manager(store_and_ledger, tmp_path, limit=2)
    mgr.start("claude", "t1", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    mgr.start("claude", "t2", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    sid3 = reserve_start(ledger)
    with pytest.raises(RuntimeError, match="concurrency_limit=2 reached"):
        mgr.start("claude", "t3", cwd, owner_id=OWNER, session_id=sid3)
    created[0].control_state = "idle"
    out = mgr.start("claude", "t3", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    assert out.session_id is not None
    assert created[0].control_state == "idle"


def test_awaiting_user_still_counts_as_active(tmp_path, store_and_ledger):
    cwd = str(tmp_path)
    mgr, created, ledger = _manager(store_and_ledger, tmp_path, limit=2)
    mgr.start("claude", "t1", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    mgr.start("claude", "t2", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    created[0].control_state = "awaiting_user"
    sid3 = reserve_start(ledger)
    with pytest.raises(RuntimeError, match="concurrency_limit=2 reached"):
        mgr.start("claude", "t3", cwd, owner_id=OWNER, session_id=sid3)


def test_intervention_required_still_counts_as_active(tmp_path, store_and_ledger):
    cwd = str(tmp_path)
    mgr, created, ledger = _manager(store_and_ledger, tmp_path, limit=2)
    mgr.start("claude", "t1", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    mgr.start("claude", "t2", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    created[0].control_state = "intervention_required"
    sid3 = reserve_start(ledger)
    with pytest.raises(RuntimeError, match="concurrency_limit=2 reached"):
        mgr.start("claude", "t3", cwd, owner_id=OWNER, session_id=sid3)


def test_active_count_reflects_only_non_idle_sessions(tmp_path, store_and_ledger):
    cwd = str(tmp_path)
    mgr, created, ledger = _manager(store_and_ledger, tmp_path, limit=3)
    mgr.start("claude", "t1", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    mgr.start("claude", "t2", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    assert mgr._active_count() == 2
    created[0].control_state = "idle"
    assert mgr._active_count() == 1


# ---- idle_retained_limit ----

def test_idle_retained_limit_defaults_to_concurrency_limit(tmp_path, store_and_ledger):
    mgr, _, _ = _manager(store_and_ledger, tmp_path, limit=4)
    assert mgr._idle_limit == 4


def test_idle_retained_limit_enforced(tmp_path, store_and_ledger):
    cwd = str(tmp_path)
    mgr, created, ledger = _manager(store_and_ledger, tmp_path, limit=5, idle_retained_limit=1)
    mgr.start("claude", "t1", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    created[0].control_state = "idle"
    sid2 = reserve_start(ledger)
    with pytest.raises(RuntimeError, match="idle_retained_limit=1"):
        mgr.start("claude", "t2", cwd, owner_id=OWNER, session_id=sid2)


# ---- config loader ----

def _toml(tmp_path, body):
    p = tmp_path / "nelix.toml"
    p.write_text(body)
    return str(p)


def test_load_idle_retained_limit_defaults_to_given_default(tmp_path):
    path = _toml(tmp_path, "concurrency_limit = 3\n")
    assert load_idle_retained_limit(path, default=3) == 3


def test_load_idle_retained_limit_explicit_value(tmp_path):
    path = _toml(tmp_path, "idle_retained_limit = 7\n")
    assert load_idle_retained_limit(path, default=5) == 7


def test_load_idle_retained_limit_invalid_falls_back(tmp_path):
    path = _toml(tmp_path, 'idle_retained_limit = "lots"\n')
    assert load_idle_retained_limit(path, default=5) == 5


def test_load_idle_retained_limit_missing_file(tmp_path):
    assert load_idle_retained_limit(str(tmp_path / "nope.toml"), default=6) == 6


# ---- Task 10: manager.send_turn re-acquire + respond routing ----

def test_send_turn_resumes_idle_session(tmp_path, store_and_ledger):
    cwd = str(tmp_path)
    mgr, created, ledger = _manager(store_and_ledger, tmp_path, limit=2)
    mgr.start("claude", "t1", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    created[0].control_state = "idle"
    out = mgr.send_turn(created[0]._id, "keep going")
    assert out.status == "resumed"
    assert created[0].sent == ["keep going"]
    assert created[0].control_state == "busy"


def test_send_turn_unknown_session(tmp_path, store_and_ledger):
    mgr, _, _ = _manager(store_and_ledger, tmp_path, limit=2)
    assert mgr.send_turn("s-nope", "hi").status == "unknown_session"


def test_send_turn_refused_when_active_cap_full(tmp_path, store_and_ledger):
    cwd = str(tmp_path)
    mgr, created, ledger = _manager(store_and_ledger, tmp_path, limit=2)
    mgr.start("claude", "t1", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    mgr.start("claude", "t2", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    created[0].control_state = "idle"
    mgr.start("claude", "t3", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    out = mgr.send_turn(created[0]._id, "resume me")
    assert out.status != "resumed"
    assert created[0].sent == []
    assert created[0].control_state == "idle"


def test_respond_on_idle_routes_to_send_turn(tmp_path, store_and_ledger):
    cwd = str(tmp_path)
    mgr, created, ledger = _manager(store_and_ledger, tmp_path, limit=2)
    mgr.start("claude", "t1", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    created[0].control_state = "idle"
    out = mgr.respond(created[0]._id, "next task", owner_id=OWNER)
    assert out.status == "resumed"
    assert created[0].sent == ["next task"]
    assert created[0].responded == []


def test_respond_on_awaiting_user_uses_respond(tmp_path, store_and_ledger):
    cwd = str(tmp_path)
    mgr, created, ledger = _manager(store_and_ledger, tmp_path, limit=2)
    mgr.start("claude", "t1", cwd, owner_id=OWNER, session_id=reserve_start(ledger))
    created[0].control_state = "awaiting_user"
    out = mgr.respond(created[0]._id, "1", owner_id=OWNER)
    assert out.status == "resumed"
    assert created[0].responded == ["1"]
    assert created[0].sent == []


def test_respond_unknown_session_still_unknown(tmp_path, store_and_ledger):
    mgr, _, _ = _manager(store_and_ledger, tmp_path, limit=2)
    assert mgr.respond("s-nope", "x", owner_id=OWNER).status == "unknown_session"
