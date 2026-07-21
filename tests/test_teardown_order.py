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
import traceback
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


# ── Integration test: _reap_orphan with live monitor ──────────────────

class _MonitoredSession:
    """Session mock with a live monitor thread pumping a _GapHandle from a
    separate thread. Exposes the same private attributes _reap_orphan reads
    (_lock, _finalized, _closing, _stop, _thread, _launcher, _handle,
    executor, snapshot(), orphan_marked_ts(), last_observed()) plus _exc /
    _exc_text captured on monitor crash."""

    def __init__(self, sid, ex, spec, ev, clock=None, handle=None):
        self._id = sid
        self.executor = ex
        self._spec = spec
        self._events_queue = ev
        self.on_terminal = None
        self.deliver_turn = None
        self._persist_terminal = None
        self.lineage_id = sid
        self.restarted_from = None
        self.restart_count = 0
        self.model = None
        self._last_screen_excerpt = "live session output"
        self._obs_clock = clock if clock is not None else time.time
        self._lock = threading.Lock()
        self._last_observed = self._obs_clock()
        self._orphan_marked_ts = None
        self._state = "awaiting_user"
        self._terminal_kind = None
        self._closing = False
        self._finalized = False
        self._stop = threading.Event()
        self._exc = None
        self._exc_text = None
        self._handle = handle or _GapHandle(sleep=0.5)
        self._launcher = _NoopLauncher(self._handle)
        self._thread = None

    def start(self, task, cwd):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            while not self._stop.is_set():
                self._handle.pump()
        except Exception:
            self._exc = sys.exc_info()
            self._exc_text = traceback.format_exc()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        return {
            "session_id": self._id, "executor": self.executor,
            "control_state": self._state, "task_delivery": "delivered",
            "terminal": self._terminal_kind is not None,
            "screen_excerpt": self._last_screen_excerpt,
            "text": self._last_screen_excerpt,
        }

    def terminal_snapshot(self):
        return {
            "session_id": self._id,
            "terminal_kind": self._terminal_kind or "orphan_reaped",
            "screen_excerpt": self._last_screen_excerpt,
            "lineage_id": self.lineage_id,
        }

    def pending_async_id(self):
        return None

    def observe(self):
        with self._lock:
            self._last_observed = self._obs_clock()
            self._orphan_marked_ts = None

    def mark_orphaned(self, grace):
        if grace <= 0:
            return
        with self._lock:
            now = self._obs_clock()
            if now - self._last_observed < grace:
                return
            if self._orphan_marked_ts is None:
                self._orphan_marked_ts = now

    def last_observed(self):
        with self._lock:
            return self._last_observed

    def orphan_marked_ts(self):
        with self._lock:
            return self._orphan_marked_ts


def test_reap_orphan_monitor_exits_cleanly(tmp_path):
    """When the REAL _reap_orphan path executes its join-before-close
    teardown on a session with a live monitor pumping a PTY, the monitor
    exits cleanly (_exc is None).

    This test calls the actual manager._reap_orphan (not a hand-built
    teardown sequence inside the test), so it covers the PRODUCTION wiring
    — unlike the two isolation tests above which prove the PRINCIPLE but
    do not prove the manager uses the correct order.

    SELF-CHECK (apply the mutation to verify this test is real):
      Move sess._thread.join(timeout=10) AFTER the close() block in
      daemon/manager.py _reap_orphan.  This test MUST fail.
      Revert -> passes.
    """
    import paths
    from nelix_store.store import Store
    from nelix_store.ledger import StartLedger
    from tests.conftest import EXECUTOR, OWNER, make_spec as _make_spec
    from daemon.manager import SessionManager

    # _TrackingEventQueue  (inline — avoids cross-test import)
    class _TrackingEventQueue:
        def __init__(self):
            self._publish_calls = []
        def publish(self, *a, **kw):
            self._publish_calls.append(a)
        def latest_seq(self, *a): return 0
        def latest_seqs(self, *a): return {}
        def wait_event(self, *a, **kw): return None
        def wait_event_any(self, *a, **kw): return None
        def forget_session(self, *a): pass
        def resolve_decision(self, *a, **kw): pass

    class _FakeClock:
        def __init__(self, t=1000.0, step=0.1):
            self.t = t
            self.step = step
        def __call__(self):
            v = self.t
            self.t += self.step
            return v

    store = Store(paths.nelix_root(), clock=lambda: 1000.0)
    try:
        clock = _FakeClock(1000.0)
        ledger = StartLedger(paths.nelix_root(), clock=clock)

        events = _TrackingEventQueue()
        handle = _GapHandle(sleep=0.5)

        def sf(sid, ex, spec, ev):
            return _MonitoredSession(sid, ex, spec, ev, clock=clock,
                                     handle=handle)

        # Reserve a start (creates start row for the store).
        sid = _make_sid(ledger, store)

        mgr = SessionManager(
            {EXECUTOR: _make_spec(observation_grace_seconds=0.5)},
            events, store,
            session_factory=sf,
            concurrency_limit=5, terminal_snapshot_ttl=300.0, clock=clock,
        )
        mgr.start(EXECUTOR, "test", ".", owner_id=OWNER, session_id=sid)
        sess = mgr.get(sid)
        assert sess is not None, "session registered after start"

        # Arm the gap: next pump() will signal entry + sleep.
        handle.arm()
        assert handle.entered.wait(timeout=5.0), (
            "monitor never entered blocked pump")
        # Monitor is now inside pump(), blocked for the sleep window.

        # Advance clock past the 0.5 s grace and mark as orphaned.
        clock.t = 1001.0
        mgr._check_orphans()
        assert sess.orphan_marked_ts() is not None, (
            "session should be marked orphaned after grace")

        # Advance past grace a second time, then reap via the REAL path.
        clock.t = 1002.0
        reaped = mgr._reap_orphan(sid)
        assert reaped, "_reap_orphan should return True"

        # The REAL _reap_orphan joined the monitor BEFORE closing the
        # handle, so the monitor exited cleanly.
        assert sess._exc is None, (
            f"Monitor crash in _reap_orphan — use-after-close race: "
            f"{sess._exc_text}")

    finally:
        store.close()


def _make_sid(ledger, _store, owner_id="test-owner"):
    import uuid
    key = f"k-{uuid.uuid4().hex[:8]}"
    r = ledger.reserve(idempotency_key=key, owner_id=owner_id,
                       orchestration_id="o-" + "c" * 32,
                       request_fingerprint="fp")
    ledger.assign_generation(r.session_id, "g-" + "a" * 32,
                             "g-" + "0" * 32)
    return r.session_id
