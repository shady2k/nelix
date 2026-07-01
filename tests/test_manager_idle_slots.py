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


class FakeSession:
    """Manager-facing session double with a mutable `control_state` so a test can drive it from
    active (busy/awaiting_user) to `idle`."""
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

    def start(self, task, cwd):
        self.started = True

    def stop(self):
        if self.on_terminal is not None:
            self.on_terminal(self._id)

    def snapshot(self):
        return {"session_id": self._id, "executor": self.executor,
                "control_state": self.control_state, "task_delivery": "delivered",
                "pending": self.control_state == "awaiting_user"}


def _manager(tmp_path, limit=2, idle_retained_limit=None):
    specs = {"claude": ExecutorSpec(command="claude", args=[], env={}, driver="claude")}
    events = EventQueue()
    created = []

    def factory(sid, ex, spec, ev):
        s = FakeSession(sid, ex, spec)
        created.append(s)
        return s

    mgr = SessionManager(specs, events, concurrency_limit=limit,
                         idle_retained_limit=idle_retained_limit,
                         session_factory=factory,
                         session_retain=0, session_max_age_days=0)
    return mgr, created


# ---- active-vs-live counting ----

def test_idle_session_frees_an_active_slot(tmp_path):
    cwd = str(tmp_path)
    mgr, created = _manager(tmp_path, limit=2)
    mgr.start("claude", "t1", cwd)
    mgr.start("claude", "t2", cwd)
    with pytest.raises(RuntimeError, match="concurrency_limit=2 reached"):
        mgr.start("claude", "t3", cwd)               # 2 active -> at cap
    created[0].control_state = "idle"                # session 1 finished its turn, stays alive
    out = mgr.start("claude", "t3", cwd)             # idle no longer occupies an active slot
    assert out.session_id is not None
    assert created[0].control_state == "idle"        # the idle session is still retained (not evicted)


def test_awaiting_user_still_counts_as_active(tmp_path):
    # A respondable pause (awaiting_user) is live work, NOT free capacity: it still holds its slot.
    cwd = str(tmp_path)
    mgr, created = _manager(tmp_path, limit=2)
    mgr.start("claude", "t1", cwd)
    mgr.start("claude", "t2", cwd)
    created[0].control_state = "awaiting_user"
    with pytest.raises(RuntimeError, match="concurrency_limit=2 reached"):
        mgr.start("claude", "t3", cwd)


def test_intervention_required_still_counts_as_active(tmp_path):
    # A stuck/hung agent (intervention_required) still holds a live PTY -> NOT a free slot.
    cwd = str(tmp_path)
    mgr, created = _manager(tmp_path, limit=2)
    mgr.start("claude", "t1", cwd)
    mgr.start("claude", "t2", cwd)
    created[0].control_state = "intervention_required"
    with pytest.raises(RuntimeError, match="concurrency_limit=2 reached"):
        mgr.start("claude", "t3", cwd)


def test_active_count_reflects_only_non_idle_sessions(tmp_path):
    cwd = str(tmp_path)
    mgr, created = _manager(tmp_path, limit=3)
    mgr.start("claude", "t1", cwd)
    mgr.start("claude", "t2", cwd)
    assert mgr._active_count() == 2
    created[0].control_state = "idle"
    assert mgr._active_count() == 1                  # the idle one dropped out of the active count


# ---- idle_retained_limit ----

def test_idle_retained_limit_defaults_to_concurrency_limit(tmp_path):
    mgr, _ = _manager(tmp_path, limit=4)             # idle_retained_limit unset -> defaults
    assert mgr._idle_limit == 4


def test_idle_retained_limit_enforced(tmp_path):
    cwd = str(tmp_path)
    mgr, created = _manager(tmp_path, limit=5, idle_retained_limit=1)
    mgr.start("claude", "t1", cwd)
    created[0].control_state = "idle"                # 1 idle == idle_retained_limit, active slots free
    with pytest.raises(RuntimeError, match="idle_retained_limit=1"):
        mgr.start("claude", "t2", cwd)               # too many retained idle sessions


# ---- config loader ----

def _toml(tmp_path, body):
    p = tmp_path / "nelix.toml"
    p.write_text(body)
    return str(p)


def test_load_idle_retained_limit_defaults_to_given_default(tmp_path):
    path = _toml(tmp_path, "concurrency_limit = 3\n")     # no idle_retained_limit key
    assert load_idle_retained_limit(path, default=3) == 3


def test_load_idle_retained_limit_explicit_value(tmp_path):
    path = _toml(tmp_path, "idle_retained_limit = 7\n")
    assert load_idle_retained_limit(path, default=5) == 7


def test_load_idle_retained_limit_invalid_falls_back(tmp_path):
    path = _toml(tmp_path, 'idle_retained_limit = "lots"\n')
    assert load_idle_retained_limit(path, default=5) == 5


def test_load_idle_retained_limit_missing_file(tmp_path):
    assert load_idle_retained_limit(str(tmp_path / "nope.toml"), default=6) == 6
