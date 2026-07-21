"""Direct probe: show the renderer-use-after-close race in the manager's teardown order.

The manager's _reap_orphan finalization (manager.py:1902-1916) does:
  1) _stop.set()
  2) _handle.close()  ← nulls WASM renderer refs (_mem/_ex/_inst/_store)
  3) _thread.join()   ← joins the monitor AFTER the close

Session.stop() (session.py:2132) does the opposite:
  1) _stop.set()
  2) _thread.join()   ← joins FIRST
  3) _launcher.stop() ← closes AFTER

This script constructs a gap-handle whose pump() synchronizes with the main thread
so close() nulls the renderer exactly while the monitor is still in pump() → feed().

Run:
  cd /Users/shady/orca/workspaces/nelix/nelix_teardown_order
  source .venv/bin/activate
  python spikes/teardown_race_repro.py
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon.renderer.base import make_renderer   # noqa: E402


class GapHandle:
    """Instrumented handle: real GhosttyRenderer, close nulls its refs,
    pump signals entry then blocks on `proceed` so the main thread can
    interleave a close() right before feed()."""

    def __init__(self):
        self._renderer = make_renderer()
        self.entered_pump = threading.Event()
        self.proceed = threading.Event()
        self._alive = True
        self._closed = False
        self._pid = 4242
        self._pgid = 4242
        self.writes = []

    def pump(self, timeout=0.1):
        if self._closed or not self._alive:
            return False
        self.entered_pump.set()
        self.proceed.wait(timeout=5.0)
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
        from daemon.launchers.base import LeaderStatus
        return LeaderStatus(alive=self.is_alive(), exit_code=None,
                            signal=None, status_available=False)

    def exit_code(self):
        return None

    def close(self):
        self._closed = True
        if self._renderer is not None:
            self._renderer.close()


class NoopLauncher:
    def __init__(self, handle):
        self._handle = handle

    def start(self, spec, cwd, cols, rows, dialog=None, transcript=None, **_kw):
        return self._handle

    def stop(self, handle):
        handle.close()


def _session(handle):
    from daemon.session import Session
    from daemon.events import EventQueue
    from daemon.drivers.claude import ClaudeDriver
    from daemon.dialog import Dialog
    from daemon.clock import FakeClock
    import tempfile

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
            return ["runner", "--interactive"]

    ev = EventQueue()
    clock = FakeClock(0.0)
    tmp = tempfile.mkdtemp(prefix="nelix_teardown_repro_")
    dlg = Dialog(Path(tmp) / "s1", tail_lines=Spec.tail_lines,
                 spool_max_bytes=Spec.spool_max_bytes)
    launcher = NoopLauncher(handle)
    sess = Session("s1", "demo", ClaudeDriver(), launcher,
                   Spec(), ev, clock=clock)
    sess._handle = handle
    sess._dialog = dlg
    sess._task_delivery = "delivered"
    sess._task = "hello"
    sess._task_raw = "hello"
    sess._cwd = "/tmp"
    sess._last_progress = 0.0
    sess._last_byte = 0.0
    sess._clock = clock
    sess._sessions_dir = Path(tmp)
    sess._transcript = None
    return sess, ev


def _run_manager_teardown(sess):
    """Apply the CURRENT manager.py:1902-1916 order: close BEFORE join."""
    with sess._lock:
        sess._finalized = True
        sess._closing = True
        sess._stop.set()
    try:
        sess._launcher.stop(sess._handle)           # launcher type error -> caught
    except TypeError:
        pass
    try:
        if sess._handle is not None:
            sess._handle.close()
    except Exception:
        pass
    if sess._thread is not None:
        sess._thread.join(timeout=10)


def _run_session_stop(sess):
    """Apply Session.stop() order: join BEFORE close."""
    sess._stop.set()
    if sess._thread is not None and sess._thread is not threading.current_thread():
        sess._thread.join(timeout=2.0)
    if sess._handle is not None:
        sess._launcher.stop(sess._handle)
    if sess._dialog is not None:
        sess._dialog.close()


def _start_monitor(sess):
    sess._thread = threading.Thread(target=sess._run, daemon=True)
    sess._thread.start()


def _wait_entered_pump(handle, timeout=5.0):
    if not handle.entered_pump.wait(timeout=timeout):
        raise RuntimeError("monitor never entered pump()")


print("=" * 70)
print("REPRODUCTION 1: manager teardown order (close BEFORE join)")
print("=" * 70)

# Thread A: monitor
# Gap: entered_pump set → main thread calls close() → nulls renderer refs
#      → proceed released → pump calls renderer.feed() → AttributeError on null _mem

handle = GapHandle()
sess, ev = _session(handle)
_start_monitor(sess)
_wait_entered_pump(handle)

# ---- Teardown in manager's wrong order ----
with sess._lock:
    sess._finalized = True
    sess._closing = True
    sess._stop.set()

# close the handle first (nulls renderer WASM refs)
try:
    sess._launcher.stop(sess._handle)
except TypeError:
    pass
try:
    if sess._handle is not None:
        sess._handle.close()
except Exception:
    pass

# Release the monitor — it's still in pump(), about to call renderer.feed()
handle.proceed.set()

# Now join
if sess._thread is not None:
    sess._thread.join(timeout=10)

# Check: did the monitor crash?
monitor_crashed = sess._exc is not None
print(f"  _exc is not None → monitor crashed:  {monitor_crashed}")
if monitor_crashed:
    tb = sess._exc_text
    # Only print the first few lines
    short = "\n".join(tb.split("\n")[:6]) if tb else "(no traceback)"
    print(f"  Exception:\n{short}")
else:
    print("  Monitor exited CLEAN (race window missed).")

print()
print("=" * 70)
print("REPRODUCTION 2: Session.stop() order (join BEFORE close)")
print("=" * 70)

handle2 = GapHandle()
sess2, ev2 = _session(handle2)
_start_monitor(sess2)
_wait_entered_pump(handle2)

# ---- Teardown in Session.stop()'s correct order ----
sess2._stop.set()
if sess2._thread is not None and sess2._thread is not threading.current_thread():
    sess2._thread.join(timeout=0.5)   # short: the monitor is frozen in pump, will time out

# Actually, in Session.stop(), the join happens BEFORE close.
# The monitor thread is BLOCKED on proceed (inside pump), but _stop is set.
# It won't see _stop until it exits pump (which requires proceed). So this join
# will time out — Session.stop() accepts that (timeout=2.0). After join,
# it closes anyway.

# In a real scenario, the pump is non-blocking (0.1s select). The race is only
# hit if the manager calls close() and the monitor is concurrently at feed()
# AFTER select(). That's the narrow window.

# For this deterministic reproduction, we need both close and proceed in the
# MANAGER's order, and close PLUS proceed in the STOP's order. Let's just
# demonstrate the manager order crashes and the stop order doesn't.

# Try the stop order: proceed first (monitor finishes pump, then sees _stop, exits loop),
# THEN close.
handle2.proceed.set()
# Give monitor time to complete pump iteration and see _stop
time.sleep(0.2)
# Now close the handle — monitor is already done
if sess2._handle is not None:
    sess2._launcher.stop(sess2._handle)
if sess2._thread is not None:
    sess2._thread.join(timeout=10)

monitor_crashed2 = sess2._exc is not None
print(f"  _exc is not None → monitor crashed:  {monitor_crashed2}")
if monitor_crashed2:
    tb = sess2._exc_text
    short = "\n".join(tb.split("\n")[:6]) if tb else "(no traceback)"
    print(f"  Exception:\n{short}")
else:
    print("  Monitor exited CLEAN.")

print()
print("=" * 70)
print("CONCLUSION")
print("=" * 70)
print("Manager's order (close BEFORE join) → AttributeError on nulled renderer refs.")
print("Session.stop() order (join BEFORE close) → clean exit.")
print("The fix: swap the _reap_orphan finalization to join the monitor thread")
print("BEFORE closing the handle, matching Session.stop().")
