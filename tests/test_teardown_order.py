"""Test teardown ordering: join the monitor thread BEFORE closing the handle.

The manager's _reap_orphan used to close the handle (nulling WASM renderer refs)
before joining the monitor thread, creating a use-after-close race: the monitor
could be inside pump() -> renderer.feed() exactly when close() nulls _mem/_ex,
resulting in TypeError/AttributeError on the freed renderer.

The observable consequence: the monitor's _exc is set (crash traceback) vs None
(clean exit). This test uses a gap-handle that blocks inside pump() to make the
race deterministic.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon.renderer.base import make_renderer
from daemon.launchers.base import LeaderStatus
from daemon.session import Session
from daemon.events import EventQueue
from daemon.drivers.claude import ClaudeDriver
from daemon.dialog import Dialog
from daemon.clock import FakeClock


class _GapHandle:
    """Handle with a real GhosttyRenderer. 'entered' set when pump() is called
    (before feeding the renderer). pump() then sleeps for a fixed window so the
    test thread can interleave a close() deterministically."""

    def __init__(self, sleep=0.5):
        self._renderer = make_renderer()
        self.entered = threading.Event()
        self._sleep = sleep
        self._enabled = False       # only block pump after _wait_until_ready
        self._alive = True
        self._closed = False
        self._pid = 4242
        self._pgid = 4242
        self.writes = []

    def arm(self):
        """Arm the next pump call to signal + block."""
        self._enabled = True
        self.entered.clear()

    def pump(self, timeout=0.1):
        if self._closed or not self._alive:
            return False
        if self._enabled:
            self.entered.set()
            time.sleep(self._sleep)
            self._enabled = False
        if self._renderer is not None:
            self._renderer.feed(b"hello\n")
        return True

    def render(self):
        if self._renderer is not None:
            return self._renderer.render()
        return ""

    def is_alive(self):
        return self._alive and not self._closed

    def write(self, data, timeout=None, drain_output=False):
        self.writes.append(data)

    def finalize(self):
        pass

    def leader_pid(self):
        return self._pid

    def leader_pgid(self):
        return self._pgid

    def assert_leader_is_group_leader(self):
        pass

    def leader_status(self):
        return LeaderStatus(alive=self.is_alive(), exit_code=None,
                            signal=None, status_available=False)

    def exit_code(self):
        return None

    def close(self):
        self._closed = True
        if self._renderer is not None:
            self._renderer.close()


class _NoopLauncher:
    def __init__(self, handle):
        self._handle = handle

    def start(self, spec, cwd, cols, rows, dialog=None, transcript=None, **_kw):
        return self._handle

    def stop(self, handle):
        handle.close()


class _Spec:
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
        return ["runner", "--interactive"]


def _make_session(handle, tmp_path):
    """Create a real Session with a gap-handle, post-delivery mode.
    The session will skip _wait_until_ready and enter _loop directly, so the
    first pump() in _loop hits our gap."""
    ev = EventQueue()
    clock = FakeClock(0.0)
    dlg = Dialog(Path(tmp_path) / "s1", tail_lines=_Spec.tail_lines,
                 spool_max_bytes=_Spec.spool_max_bytes)
    launcher = _NoopLauncher(handle)
    sess = Session("s1", "demo", ClaudeDriver(), launcher, _Spec(), ev, clock=clock)
    # Bypass the real start() flow: set everything up so _run skips delivery
    # wait (task_delivery=delivered) and enters _loop directly after the now-
    # fast _wait_until_ready stabilises on the repeated "hello" frame.
    sess._handle = handle
    sess._dialog = dlg
    sess._task_delivery = "delivered"
    sess._task = "hello"
    sess._task_raw = "hello"
    sess._cwd = "/tmp"
    sess._last_progress = 0.0
    sess._last_byte = 0.0
    sess._clock = clock
    sess._sessions_dir = Path(tmp_path)
    sess._transcript = None
    sess._spawn_ts = time.time()
    return sess, ev


def _start_monitor(sess):
    sess._thread = threading.Thread(target=sess._run, daemon=True)
    sess._thread.start()


# ── Test: correct order (Session.stop / fixed _reap_orphan) ──────────

def test_teardown_join_before_close_does_not_crash_monitor(tmp_path):
    """When the teardown joins the monitor BEFORE closing the handle, the
    monitor exits cleanly (Session._exc is None). This is the order Session.stop()
    and the fixed _reap_orphan use."""
    handle = _GapHandle(sleep=0.8)
    sess, _ev = _make_session(handle, tmp_path)
    _start_monitor(sess)

    # Wait for _wait_until_ready to settle, then arm the gap in _loop
    time.sleep(2.0)
    handle.arm()

    # Monitor is now inside pump, blocked for the sleep window.
    # Wait for it to confirm entry.
    assert handle.entered.wait(timeout=3.0), "monitor never entered blocked pump"

    # ── Correct teardown: join BEFORE close ──
    with sess._lock:
        sess._finalized = True
        sess._closing = True
        sess._stop.set()
    if sess._thread is not None and sess._thread is not threading.current_thread():
        sess._thread.join(timeout=10)
    try:
        sess._launcher.stop(sess._handle)
    except Exception:
        pass
    try:
        if sess._handle is not None:
            sess._handle.close()
    except Exception:
        pass

    assert sess._exc is None, (
        f"Monitor crash — use-after-close race: {sess._exc_text}")


# ── Test: wrong order (old _reap_orphan) ────────────────────────────

def test_teardown_close_before_join_crashes_monitor(tmp_path):
    """When the teardown closes the handle BEFORE joining the monitor, the
    monitor can be inside pump -> renderer.feed while the WASM refs are nulled,
    causing TypeError on self._ex (NoneType is not subscriptable).

    This test PROVES the bug exists, then the fix prevents it."""

    handle = _GapHandle(sleep=0.8)
    sess, _ev = _make_session(handle, tmp_path)
    _start_monitor(sess)

    # Wait for _wait_until_ready to settle, then arm the gap in _loop
    time.sleep(2.0)
    handle.arm()

    # Monitor is now inside pump, blocked for the sleep window.
    # Wait for it to confirm entry.
    assert handle.entered.wait(timeout=3.0), "monitor never entered blocked pump"

    # ── Wrong teardown: close BEFORE join (old _reap_orphan order) ──
    with sess._lock:
        sess._finalized = True
        sess._closing = True
        sess._stop.set()
    try:
        sess._launcher.stop(sess._handle)
    except TypeError:
        pass    # old code didn't pass handle arg
    except Exception:
        pass
    try:
        if sess._handle is not None:
            sess._handle.close()
    except Exception:
        pass
    if sess._thread is not None:
        sess._thread.join(timeout=10)

    assert sess._exc is not None, (
        "Expected monitor crash from use-after-close race, but _exc is None "
        "(race window missed — increase sleep in _GapHandle)")
