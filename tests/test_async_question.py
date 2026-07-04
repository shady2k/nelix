"""Async-question slot on Session (Task 3): an executor's non-blocking `question` (message-plane,
executor -> orchestrator) wakes the orchestrator via ONE EventQueue publish (tripping the
already-armed nelix-wait waiter) but is served out of its OWN slot (self._async_question), never
installed into self._decision -- that slot's supersede logic (in Session._publish) is built for
the single blocking pause and would clobber/mask a real decision (or be masked by one).
kind="async_question" is deliberately NOT in RESPONDABLE_KINDS, so EventQueue.pending() keeps
meaning "blocking decision only" (the phantom-blocked invariant, 92a0dc6, must not be reopened for
this new channel).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from daemon.clock import FakeClock              # noqa: E402
from daemon.config import ExecutorSpec          # noqa: E402
from daemon.dialog import Dialog                # noqa: E402
from daemon.drivers.claude import ClaudeDriver  # noqa: E402
from daemon.events import EventQueue, RESPONDABLE_KINDS  # noqa: E402
from daemon.manager import SessionManager       # noqa: E402
from daemon.messages import AsyncQuestion, ProgressNote  # noqa: E402
from daemon.session import Session              # noqa: E402


class Spec:
    driver = "claude"
    settle_seconds = 1.5
    respond_write_seconds = 5.0
    respond_confirm_seconds = 0.3
    delivery_confirm_seconds = 2.0
    max_idle_seconds = 600.0
    startup_timeout_seconds = 60.0
    tail_lines = 100
    status_tail_chars = 4000
    spool_max_bytes = 1_000_000

    def argv(self):
        return ["runner", "--interactive"]   # fictional; mirrors ExecutorSpec.argv()


class FakeHandle:
    """Scripted PTY (same idiom as tests/test_session.py's FakeHandle): render() walks `frames`;
    each pump() advances the injected FakeClock by `step` so the belief engine's settle/grace
    windows elapse deterministically (no real sleeps, no time.* in the belief path)."""
    def __init__(self, frames, stop=None, clock=None, step=1.0):
        self.frames = frames
        self.i = -1
        self.writes = []
        self._stop = stop
        self._clock = clock
        self._step = step

    def pump(self, timeout=0.1):
        self.i += 1
        if self._clock is not None:
            self._clock.advance(self._step)
        if self._stop is not None and self.i >= len(self.frames) - 1:
            self._stop.set()
        return True

    def render(self):
        return self.frames[min(self.i, len(self.frames) - 1)]

    def is_alive(self):
        return True

    def exit_code(self):
        return None

    def write(self, data, timeout=None, drain_output=False):
        self.writes.append(data)

    def finalize(self):
        pass

    def leader_pid(self): return 4242
    def leader_pgid(self): return 4242
    def assert_leader_is_group_leader(self): pass

    def leader_status(self):
        from daemon.launchers.base import LeaderStatus
        return LeaderStatus(alive=True, exit_code=None, signal=None, status_available=False)

    def close(self):
        pass


def _make_session(tmp_path, frames):
    ev = EventQueue()
    clock = FakeClock(0.0)
    sess = Session("s1", "demo", ClaudeDriver(), None, Spec(), ev, clock=clock)
    sess._handle = FakeHandle(list(frames), stop=sess._stop, clock=clock)
    sess._dialog = Dialog(tmp_path / "s1", tail_lines=Spec.tail_lines,
                          spool_max_bytes=Spec.spool_max_bytes)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "delivered"       # drive the post-delivery run loop directly
    return sess, ev


@pytest.fixture
def session_busy(tmp_path):
    """Actively working: no pending decision, control_state=busy — the low-information "still
    working" snapshot branch that an outstanding async question must suppress."""
    sess, _ = _make_session(tmp_path, ["compiling…", "compiling…"])
    sess._loop()
    assert sess._decision is None and sess._state == "busy"
    return sess


@pytest.fixture
def session_with_decision(tmp_path):
    """A stable idle prompt publishes exactly one waiting_for_user decision -> a REAL blocking
    decision installed in _decision (driven through the real belief/publish path, not hand-set),
    so the coexistence test exercises the actual install/supersede logic in _publish."""
    box = "Here is my answer.\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"
    sess, _ = _make_session(tmp_path, ["thinking… esc to interrupt", box, box, box])
    sess._loop()
    assert sess._decision is not None
    return sess


def test_kind_stays_out_of_respondable():
    assert "async_question" not in RESPONDABLE_KINDS   # pending() must keep meaning "blocking"


def test_record_publishes_wake_event(session_busy):
    before = session_busy._events.latest_seq()
    qid, err = session_busy.record_async_question(AsyncQuestion("a or b?", "keep coding", "a", None))
    assert err is None and qid == "q_1"
    assert session_busy._events.latest_seq() == before + 1        # wake event published
    assert session_busy.snapshot()["async_question"]["question"] == "a or b?"


def test_wake_event_kind_stays_out_of_pending(session_busy):
    session_busy.record_async_question(AsyncQuestion("a or b?", "keep coding", None, None))
    evt = session_busy._events.latest_after(0)
    assert evt.kind == "async_question"
    assert session_busy._events.pending() is None      # never a blocking decision


def test_snapshot_excludes_internal_event_id(session_busy):
    session_busy.record_async_question(AsyncQuestion("q", "c", None, None))
    snap = session_busy.snapshot()
    assert "event_id" not in snap["async_question"]


def test_never_installed_into_decision(session_busy):
    session_busy.record_async_question(AsyncQuestion("q", "c", None, None))
    assert session_busy._decision is None
    assert "decision" not in session_busy.snapshot()


def test_coexists_with_blocking_decision(session_with_decision):
    # a real blocking decision is already installed in _decision
    qid, err = session_with_decision.record_async_question(AsyncQuestion("q", "cont", None, None))
    assert err is None and qid == "q_1"
    snap = session_with_decision.snapshot()
    assert "decision" in snap and "async_question" in snap        # neither masks the other
    assert session_with_decision._events.pending() is not None    # still the blocking decision
    assert session_with_decision._events.pending().kind in RESPONDABLE_KINDS


def test_second_question_already_pending(session_busy):
    session_busy.record_async_question(AsyncQuestion("q1", "c", None, None))
    qid, err = session_busy.record_async_question(AsyncQuestion("q2", "c", None, None))
    assert qid is None and err["id"] == "q_1"


def test_already_pending_error_truncates_question(session_busy):
    long_q = "x" * 500
    session_busy.record_async_question(AsyncQuestion(long_q, "c", None, None))
    _, err = session_busy.record_async_question(AsyncQuestion("q2", "c", None, None))
    assert err["question"] == long_q[:200]
    # the slot + snapshot themselves keep the FULL question, only the error preview is capped
    assert session_busy.snapshot()["async_question"]["question"] == long_q


def test_has_pending_async(session_busy):
    assert session_busy.has_pending_async() is False
    qid, _ = session_busy.record_async_question(AsyncQuestion("q", "c", None, None))
    assert session_busy.has_pending_async() is True
    assert session_busy.has_pending_async(qid) is True
    assert session_busy.has_pending_async("q_999") is False


def test_progress_gate_fires_for_async_question_alone(session_busy):
    session_busy.append_progress_note(ProgressNote("step 1", None))
    session_busy.record_async_question(AsyncQuestion("q", "c", None, None))
    snap = session_busy.snapshot()
    assert snap["progress"][-1]["summary"] == "step 1"


def test_still_working_message_suppressed_by_async_question(session_busy):
    snap_before = session_busy.snapshot()
    assert "message" in snap_before   # baseline: busy, no decision, no question -> low-info message
    session_busy.record_async_question(AsyncQuestion("q", "c", None, None))
    snap = session_busy.snapshot()
    assert "message" not in snap     # an outstanding question already woke the orchestrator


# ---- Manager-level entry points (Task 6): the HTTP route (Task 5) calls THESE, never the Session
# methods directly. Both look up the LIVE session and delegate; an absent/already-freed session
# gets an unknown_session-equivalent, never an AttributeError/KeyError. ----

def _demo_specs():
    return {"demo": ExecutorSpec(command="demo", args=[], env={}, driver="claude")}


@pytest.fixture
def manager_with_busy_session(tmp_path):
    sess, ev = _make_session(tmp_path, ["compiling…", "compiling…"])
    sess._loop()
    assert sess._decision is None and sess._state == "busy"
    mgr = SessionManager(_demo_specs(), ev, concurrency_limit=3,
                         session_retain=0, session_max_age_days=0)
    with mgr._lock:
        mgr._sessions[sess._id] = sess
    return mgr


def test_manager_record_and_note(manager_with_busy_session):
    mgr = manager_with_busy_session
    qid, err = mgr.record_async_question("s1", AsyncQuestion("a?", "c", None, None))
    assert err is None and qid == "q_1"
    assert mgr.append_progress_note("s1", ProgressNote("done step", None)) == 1


def test_manager_record_async_question_unknown_session():
    mgr = SessionManager(_demo_specs(), EventQueue(), concurrency_limit=1)
    qid, err = mgr.record_async_question("nope", AsyncQuestion("a?", "c", None, None))
    assert qid is None and err == {"error": "unknown_session"}


def test_manager_append_progress_note_unknown_session():
    mgr = SessionManager(_demo_specs(), EventQueue(), concurrency_limit=1)
    assert mgr.append_progress_note("nope", ProgressNote("x", None)) is None
