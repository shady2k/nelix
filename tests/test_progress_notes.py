"""Non-waking progress notes on Session (Task 2).

append_progress_note() lets an executor report incremental progress WITHOUT ever advancing the
EventQueue seq / notifying a waiter — the exact bug the phantom-blocked fix (92a0dc6) closed for
pre-delivery answers must not be reopened here for progress notes. Snapshot only surfaces the
progress list at a wake point (a pending decision, or terminal); the plain active-working branch
(the low-information "still working" message) must gain nothing, or a poller could learn something
happened without nelix ever waking it (the anti-poll invariant, session.py ~1049).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from daemon.clock import FakeClock            # noqa: E402
from daemon.config import MAX_PROGRESS_NOTES   # noqa: E402
from daemon.dialog import Dialog               # noqa: E402
from daemon.drivers.claude import ClaudeDriver  # noqa: E402
from daemon.events import EventQueue           # noqa: E402
from daemon.messages import ProgressNote       # noqa: E402
from daemon.session import Session             # noqa: E402


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
def session(tmp_path):
    """A freshly-constructed session — append/bound behavior doesn't depend on belief state."""
    sess, _ = _make_session(tmp_path, ["compiling…"])
    return sess


@pytest.fixture
def session_busy(tmp_path):
    """Actively working: no pending decision, control_state=busy — the low-information "still
    working" snapshot branch (session.py ~1049) that must gain NOTHING from progress notes."""
    sess, _ = _make_session(tmp_path, ["compiling…", "compiling…"])
    sess._loop()
    assert sess._decision is None and sess._state == "busy"
    return sess


@pytest.fixture
def session_with_decision(tmp_path):
    """A stable idle prompt publishes exactly one waiting_for_user decision -> a wake point, so
    the snapshot is allowed to carry the progress list."""
    box = "Here is my answer.\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"
    sess, _ = _make_session(tmp_path, ["thinking… esc to interrupt", box, box, box])
    sess._loop()
    assert sess._decision is not None
    return sess


def test_append_does_not_publish(session):
    before = session._events.latest_seq()
    seq = session.append_progress_note(ProgressNote("migration written", None))
    assert seq == 1
    assert session._events.latest_seq() == before          # NO event published -> no wake


def test_active_working_snapshot_has_no_progress(session_busy):
    session_busy.append_progress_note(ProgressNote("step 2", None))
    snap = session_busy.snapshot()
    assert "progress" not in snap and "progress_total" not in snap  # anti-poll: nothing


def test_event_snapshot_includes_progress(session_with_decision):
    session_with_decision.append_progress_note(ProgressNote("risk found", "locks table"))
    snap = session_with_decision.snapshot()
    assert snap["progress"][-1]["summary"] == "risk found"
    assert snap["progress_total"] == 1


def test_bound_drops_oldest(session):
    for i in range(MAX_PROGRESS_NOTES + 5):
        session.append_progress_note(ProgressNote(f"n{i}", None))
    view = session.progress_view()
    assert view["progress_retained"] == MAX_PROGRESS_NOTES
    assert view["progress_total"] == MAX_PROGRESS_NOTES + 5
