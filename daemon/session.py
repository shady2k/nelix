import os
import threading
import time
from pathlib import Path

from daemon.dialog import Dialog
from daemon.drivers.base import ClassifyCtx
from daemon.hygiene import sanitize_answer


def _sessions_root():
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(home) / "nelix" / "sessions"


class Session:
    def __init__(self, session_id, executor, driver, launcher, spec, events,
                 cols=120, rows=40, logger=None):
        self._id = session_id
        self._executor = executor
        self._driver = driver
        self._launcher = launcher
        self._spec = spec
        self._events = events
        self._cols = cols
        self._rows = rows
        self._log = logger
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._handle = None
        self._dialog = None
        self._state = "working"
        self._last_state = None
        self._decision = None          # frozen dict for the current pending stop
        self._norm = ""                # last normalized frame
        self._norm_since = None        # ts the normalized frame became stable
        self._last_progress = None     # ts of last meaningful (normalized) change (for hang)
        self._last_byte = None         # ts of last PTY byte
        self._sessions_dir = _sessions_root()
        driver._settle = spec.settle_seconds   # keep classify a pure (frame, ctx) fn

    @property
    def dialog(self):
        return self._dialog

    # ---- lifecycle ----
    def start(self, task):
        self._dialog = Dialog(self._sessions_dir / self._id,
                              tail_lines=self._spec.tail_lines,
                              spool_max_bytes=self._spec.spool_max_bytes)
        self._handle = self._launcher.start(self._spec, self._cols, self._rows,
                                            dialog=self._dialog)
        self._wait_until_ready()
        self._ensure_ask_mode()
        self._submit(task)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _submit(self, text):
        # The TUI treats CR (\r), not LF, as Enter. Type, let it render, then CR.
        self._handle.write(text)
        time.sleep(0.3)
        self._handle.write("\r")

    def _wait_until_ready(self, timeout=20.0, stable_for=1.2):
        last = None; stable_since = None
        deadline = time.time() + timeout
        while time.time() < deadline and self._handle.is_alive():
            self._handle.pump(0.1)
            grid = self._handle.render()
            if grid != last:
                last = grid; stable_since = time.time()
            elif grid.strip() and stable_since is not None and time.time() - stable_since >= stable_for:
                return

    def _ensure_ask_mode(self, attempts=4):
        # Cycle the driver's mode toggle until it reports ask-mode (not auto/plan).
        for _ in range(attempts):
            self._handle.pump(0.1)
            if self._driver.is_ask_mode(self._handle.render()):
                return
            self._handle.write(self._driver.ask_mode_toggle)
            time.sleep(0.3)

    # ---- loop ----
    def _ctx(self, now):
        return ClassifyCtx(
            stable_for=0.0 if self._norm_since is None else now - self._norm_since,
            bytes_idle_for=0.0 if self._last_byte is None else now - self._last_byte,
            child_alive=self._handle.is_alive(),
            exit_code=self._handle.exit_code(),
        )

    def _loop(self):
        self._last_progress = self._last_byte = time.time()
        while not self._stop.is_set():
            advanced = self._handle.pump(0.1)
            now = time.time()
            if advanced:
                self._last_byte = now
            frame = self._handle.render()
            norm = self._driver.normalize_frame(frame)
            if norm != self._norm:
                self._norm = norm
                self._norm_since = now
                self._last_progress = now          # meaningful (normalized) change
            state = self._driver.classify(frame, self._ctx(now))
            self._on_state(state, now)
            if state in ("crashed", "exited") or not self._handle.is_alive():
                break

    def _on_state(self, state, now):
        running = state in ("working", "quiet_working")
        # hang backstop: alive + running but no meaningful progress for hang_timeout.
        if running and now - self._last_progress > self._spec.hang_timeout:
            self._handle.write("\x1b")             # ESC to nudge the executor
            self._emit_stop("idle_prompt", hung=True)
            self._last_progress = now              # re-arm; do not ESC-storm
            return
        with self._lock:
            prev = self._last_state
            self._state = state
            self._last_state = state
        if state == prev:
            return
        if state in ("idle_prompt", "permission_prompt"):
            self._emit_stop(state, hung=False)
        elif state == "crashed":
            self._publish("crashed", hint=None, hung=False)
        elif state == "exited":
            self._publish("done", hint=None, hung=False)

    def _emit_stop(self, state, hung):
        # commit the final viewport so the turn tail is in the transcript, then freeze the range.
        self._handle.flush_viewport(self._dialog)
        hint = "needs_permission" if state == "permission_prompt" else None
        self._publish("waiting_for_user", hint=hint, hung=hung)

    def _publish(self, kind, hint, hung):
        with self._lock:
            turn = self._dialog.current_turn()
            start = self._dialog._turn_starts[turn]
            end = self._dialog.line_count()
            text = self._dialog.range_text(start, end,
                                           limit=self._spec.status_tail_chars)["text"]
            decision = {"kind": kind, "turn_index": turn, "range": (start, end),
                        "hint": hint, "hung": hung, "text": text}
            if kind == "waiting_for_user":
                self._decision = decision
        # publish OUTSIDE the session lock (lock order: never hold it across a queue publish).
        evt = self._events.publish(self._id, self._executor, kind, text[:200], self._state,
                                   turn_index=decision["turn_index"], range=decision["range"],
                                   hint=hint, hung=hung)
        if kind == "waiting_for_user":
            with self._lock:
                self._decision["event_id"] = evt.event_id
        if self._log is not None:
            self._log.audit_decision(self._id, self._executor, kind, evt.event_id, text)

    # ---- reads / control ----
    def snapshot(self):
        with self._lock:
            snap = {"session_id": self._id, "executor": self._executor,
                    "state": self._state,
                    "turn_count": self._dialog.turn_count() if self._dialog else 0}
            if self._decision is not None:
                # serve the FROZEN range, not a mutating "latest turn".
                s, e = self._decision["range"]
                snap["decision"] = {**self._decision,
                                    "text": self._dialog.range_text(
                                        s, e, limit=self._spec.status_tail_chars)["text"]}
            return snap

    def respond(self, event_id, answer):
        pending = self._events.pending(self._id)
        if pending is None or pending.event_id != event_id:
            return False                      # bind to the current pending decision
        self._events.mark_answered(event_id)
        if self._handle is not None:
            self._dialog.mark_turn_boundary()
            self._submit(sanitize_answer(answer))
            self._last_state = None
        with self._lock:
            self._decision = None
        return True

    def stop(self):
        self._stop.set()
        if self._handle is not None:
            self._launcher.stop(self._handle)
        if self._dialog is not None:
            self._dialog.close()
