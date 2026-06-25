import re
import threading
import time

import paths
from daemon.dialog import Dialog
from daemon.drivers.base import ClassifyCtx
from daemon.hygiene import prepare_pty_input


def _sessions_root():
    return paths.sessions_root()


# Box-drawing (U+2500–U+257F) + block elements (U+2580–U+259F). Purely structural framing
# glyphs — content-agnostic, no per-CLI knowledge.
_FRAME_CLASS = "─-╿▀-▟"
_FRAME_ONLY = re.compile(rf"^[\s{_FRAME_CLASS}]*$")           # whole line is blank/framing
_FRAME_EDGE = re.compile(rf"^[\s{_FRAME_CLASS}]+|[\s{_FRAME_CLASS}]+$")  # leading/trailing framing


def _clean_screen(screen):
    """Strip the terminal framing structurally: drop border/separator/blank lines and peel
    the framing off the edges of kept lines (e.g. '│ Welcome back! │' -> 'Welcome back!').
    Content glyphs outside the box/block ranges (e.g. '❯', U+276F) are preserved."""
    out = []
    for line in screen.split("\n"):
        if _FRAME_ONLY.match(line):
            continue                                          # pure border / separator / blank
        out.append(_FRAME_EDGE.sub("", line))
    return "\n".join(out)


def _excerpt(screen, max_chars):
    text = _clean_screen(screen)                              # clean structurally THEN cap
    return text[-max_chars:] if max_chars and len(text) > max_chars else text


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
        self._task = None              # held task, delivered once a real input box appears
        self._task_delivery = "pending"  # pending | delivered | failed
        self._blocked_fp = None        # normalized screen of the last emitted blocked event
        self._sessions_dir = _sessions_root()
        driver._settle = spec.settle_seconds   # keep classify a pure (frame, ctx) fn

    @property
    def dialog(self):
        return self._dialog

    # ---- lifecycle ----
    def start(self, task, cwd):
        # Non-blocking: spawn the PTY, hold the task, and let the monitor thread own
        # both delivery (deliver only into a verified input box) and the run loop.
        # /start returns once the PTY is spawned, NOT once the task is delivered.
        # Clean the held task FIRST (CLI-agnostic byte hygiene + the driver's command-prefix
        # policy): a rejected task raises before anything is spawned, and the cleaned text is
        # both what gets typed and what input_submission_present() matches against.
        self._task = prepare_pty_input(task, self._driver.command_prefixes)
        self._dialog = Dialog(self._sessions_dir / self._id,
                              tail_lines=self._spec.tail_lines,
                              spool_max_bytes=self._spec.spool_max_bytes)
        self._handle = self._launcher.start(self._spec, cwd, self._cols, self._rows,
                                            dialog=self._dialog)
        self._task_delivery = "pending"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---- low-level PTY ops (split from the old blind _submit) ----
    def _type_text(self, text):
        self._handle.write(text)

    def _press_enter(self):
        # Submit with the driver-declared submit key (CR for most TUIs, not LF).
        self._handle.write(self._driver.submit_key)

    def screen(self, raw=False):
        with self._lock:
            frame = self._handle.render() if self._handle is not None else ""
        return frame if raw else _clean_screen(frame)

    def _wait_until_ready(self, timeout=20.0, stable_for=1.2):
        last = None; stable_since = None
        deadline = time.time() + timeout
        while time.time() < deadline and self._handle.is_alive() and not self._stop.is_set():
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

    def _run(self):
        # Monitor-thread entrypoint: wait for the CLI to settle, deliver the held task
        # (delivery phase), then fall through to the normal run loop.
        self._wait_until_ready()
        self._last_progress = self._last_byte = time.time()
        while not self._stop.is_set() and self._task_delivery == "pending":
            advanced = self._handle.pump(0.1)
            now = time.time()
            if advanced:
                self._last_byte = now
            with self._lock:
                frame = self._handle.render()
            norm = self._driver.normalize_frame(frame)
            if norm != self._norm:
                self._norm = norm
                self._norm_since = now
                self._last_progress = now
            self._delivery_tick(frame, now)
            # no-progress backstop while blocked (spec §6): re-surface an unanswered blocked with
            # hung=True so Hermes is reminded; bypass the fingerprint dedup (call _publish directly).
            blocked_outstanding = self._decision is not None and self._decision["kind"] == "blocked"
            if (blocked_outstanding and self._spec.max_idle_seconds
                    and now - self._last_progress > self._spec.max_idle_seconds):
                self._publish("blocked", hint="task_not_delivered", hung=True,
                              requires_response=True, task_delivery="pending")
                self._last_progress = now              # re-arm so it doesn't re-fire every loop
            if not self._handle.is_alive():
                break
        if self._task_delivery == "delivered" and not self._stop.is_set():
            self._loop()

    def _delivery_tick(self, frame, now):
        state = self._driver.classify(frame, self._ctx(now))
        if state in ("working", "quiet_working"):
            return                                   # CLI busy / not settled: keep waiting
        if state in ("crashed", "exited") or not self._handle.is_alive():
            self._task_delivery = "failed"
            self._publish("crashed" if state == "crashed" else "done",
                          hint=None, hung=False, requires_response=False)
            self._stop.set()
            return
        if self._driver.is_accepting_input(frame):
            self._ensure_ask_mode()
            self._deliver_task()
        else:
            self._emit_blocked(frame)                # modal / onboarding / unknown

    def _deliver_task(self):
        self._type_text(self._task)
        deadline = time.time() + self._spec.delivery_confirm_seconds
        while time.time() < deadline and not self._stop.is_set():
            self._handle.pump(0.1)
            with self._lock:
                frame = self._handle.render()
            if self._driver.input_submission_present(frame, self._task):
                self._press_enter()
                self._dialog.mark_turn_boundary()    # task turn begins now
                self._task_delivery = "delivered"
                self._last_state = None
                return
        # Not confirmed within the window (a slow paste should have shown by now): give up — do NOT
        # press Enter, do NOT re-type. Mark failed so the run loop exits, and wake Hermes with a
        # non-respondable advisory; the human stops + restarts.
        self._task_delivery = "failed"
        self._handle.flush_viewport(self._dialog)
        self._publish("delivery_failed", hint="delivery_unconfirmed", hung=False,
                      requires_response=False, task_delivery="failed")

    def _loop(self):
        self._last_progress = self._last_byte = time.time()
        while not self._stop.is_set():
            advanced = self._handle.pump(0.1)
            now = time.time()
            if advanced:
                self._last_byte = now
            with self._lock:
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
        # no-progress backstop: running but no meaningful progress for max_idle_seconds (0 = off).
        # The daemon is a bridge — it reports the fact and wakes Hermes; it does NOT nudge (no ESC)
        # or act. Hermes decides (relay to the user, stop, restart).
        if running and self._spec.max_idle_seconds and now - self._last_progress > self._spec.max_idle_seconds:
            self._emit_stop("idle_prompt", hung=True)
            self._last_progress = now              # re-arm so it doesn't re-fire every loop
            return
        with self._lock:
            prev = self._last_state
            self._state = state
            self._last_state = state
        if state == prev:
            return
        if state == "idle_prompt":
            # The daemon can't reliably tell "asked a question" from "finished" (the real prompt
            # has a mode footer below the input line). Hermes reads the live screen on every wake,
            # so defaulting to waiting_for_user is fail-safe — never a silent dead-end.
            self._emit_stop("idle_prompt", hung=False)
        elif state == "permission_prompt":
            self._emit_stop("permission_prompt", hung=False)
        elif state == "crashed":
            self._publish("crashed", hint=None, hung=False, requires_response=False)
        elif state == "exited":
            self._publish("done", hint=None, hung=False, requires_response=False)

    def _emit_stop(self, state, hung):
        # commit the final viewport so the turn tail is in the transcript, then freeze the range.
        self._handle.flush_viewport(self._dialog)
        hint = "needs_permission" if state == "permission_prompt" else None
        self._publish("waiting_for_user", hint=hint, hung=hung, requires_response=True)

    def _emit_blocked(self, frame, hint="task_not_delivered"):
        # Surface a pre-delivery interstitial (modal / onboarding / unknown). Type/press NOTHING.
        # Dedup by normalized-screen fingerprint alone: emit once per distinct screen. A new
        # interstitial (different fingerprint) emits a fresh blocked; the same screen never re-spams,
        # including after the prior one is answered (the answer changes the screen anyway).
        fp = self._driver.normalize_frame(frame)
        if fp == self._blocked_fp:
            return
        self._blocked_fp = fp
        self._handle.flush_viewport(self._dialog)
        self._publish("blocked", hint=hint, hung=False, requires_response=True,
                      task_delivery="pending")

    def _publish(self, kind, hint, hung, requires_response=None, task_delivery=None):
        from daemon.events import RESPONDABLE_KINDS
        with self._lock:
            turn = self._dialog.current_turn()
            start = self._dialog._turn_starts[turn]
            end = self._dialog.line_count()
            page = self._dialog.range_text(start, end, limit=self._spec.status_tail_chars)
            text = page["text"]
            # render() directly (NOT self.screen(), which also takes self._lock -> deadlock).
            screen = _excerpt(self._handle.render() if self._handle is not None else "",
                              self._spec.status_tail_chars)
            if requires_response is None:
                requires_response = kind in RESPONDABLE_KINDS
            if task_delivery is None:
                task_delivery = self._task_delivery
            decision = {"kind": kind, "turn_index": turn, "range": (start, end),
                        "hint": hint, "hung": hung, "text": text,
                        "task_delivery": task_delivery, "requires_response": requires_response,
                        "screen_excerpt": screen,
                        "total_len": page["total_len"], "truncated": page["truncated"]}
            if kind in RESPONDABLE_KINDS:
                self._decision = decision
        # publish OUTSIDE the session lock (lock order: never hold it across a queue publish).
        evt = self._events.publish(self._id, self._executor, kind, text[:200], self._state,
                                   turn_index=decision["turn_index"], range=decision["range"],
                                   hint=hint, hung=hung, task_delivery=task_delivery,
                                   requires_response=requires_response, screen_excerpt=screen)
        if kind in RESPONDABLE_KINDS:
            with self._lock:
                self._decision["event_id"] = evt.event_id
        if self._log is not None:
            self._log.audit_decision(self._id, self._executor, kind, evt.event_id, text)

    # ---- reads / control ----
    def is_working(self):
        with self._lock:
            return self._decision is None and self._state in ("working", "quiet_working")

    def snapshot(self):
        with self._lock:
            snap = {"session_id": self._id, "executor": self._executor,
                    "state": self._state,
                    "turn_count": self._dialog.turn_count() if self._dialog else 0}
            if self._decision is not None:
                # serve the FROZEN range, not a mutating "latest turn".
                s, e = self._decision["range"]
                page = self._dialog.range_text(s, e, limit=self._spec.status_tail_chars)
                snap["decision"] = {**self._decision, "text": page["text"],
                                    "total_len": page["total_len"], "truncated": page["truncated"]}
            # active-working snapshots are deliberately low-information: no progress bait, just
            # "end your turn" — nelix wakes Hermes on the next event, so there is nothing to poll.
            snap["pending"] = self._decision is not None
            if self._decision is None and self._state in ("working", "quiet_working"):
                snap["message"] = ("Agent is still working. End your turn; nelix will wake "
                                   "you on the next event.")
            return snap

    def respond(self, event_id, answer):
        pending = self._events.pending(self._id)
        if pending is None or pending.event_id != event_id:
            return None                               # stale/unknown: bind to the current decision
        # Clean the answer BEFORE any mutation: a rejected answer (command prefix / empty after
        # sanitization) leaves the decision pending and nothing typed, so the caller can retry.
        clean = prepare_pty_input(answer, self._driver.command_prefixes)
        is_blocked = pending.kind == "blocked"
        seq = self._events.mark_answered(event_id)
        if self._handle is not None:
            if not is_blocked:
                # only a delivered agent turn gets a boundary; the monitor reads the dialog
                # under self._lock in _publish, so mutate it under the lock too.
                with self._lock:
                    self._dialog.mark_turn_boundary()
            self._type_text(clean)                     # PTY writes stay outside the lock
            self._press_enter()
            self._last_state = None
        with self._lock:
            self._decision = None
        return seq                                    # cursor for the waiter to arm past

    def stop(self):
        self._stop.set()
        # Join the monitor thread before closing the dialog so an in-flight delivery/emit
        # never writes to a closed transcript (the monitor owns all dialog writes).
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        if self._handle is not None:
            self._launcher.stop(self._handle)
        if self._dialog is not None:
            self._dialog.close()
