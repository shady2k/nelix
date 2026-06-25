import hashlib
import re
import sys
import threading
import time
import traceback

import paths
from daemon import lifecycle_log
from daemon.dialog import Dialog
from daemon.drivers.base import ClassifyCtx
from daemon.hygiene import prepare_pty_input
from daemon.errors import PtyWriteTimeout


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
        self._finalized = False        # _finish ran (idempotency guard, under self._lock)
        self._exc = None               # sys.exc_info() if the monitor body raised
        self._exc_text = None          # formatted traceback captured at catch time
        self._spawn_ts = None          # ts the PTY leader was spawned (for alive_for)
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
        self._spawn_ts = time.time()
        self._log_spawned(self._spec.argv(), type(self._launcher).__name__)
        if self._log is not None:
            self._log.debug("session", "monitor_started", session_id=self._id)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---- low-level PTY ops (split from the old blind _submit) ----
    def _type_text(self, text, timeout=None, drain_output=False):
        self._handle.write(text, timeout=timeout, drain_output=drain_output)

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
        if self._log is not None:
            self._log.warning("session", "ask_mode_failed", session_id=self._id)

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
        # (delivery phase), then fall through to the normal run loop. A try/except/finally
        # guarantees _finish() runs exactly once when the monitor exits, for ANY reason.
        try:
            self._wait_until_ready()
            if self._log is not None:
                self._log.debug("session",
                                "cli_ready" if self._handle.is_alive() else "cli_ready_timeout",
                                session_id=self._id)
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
        except Exception:
            self._exc = sys.exc_info()
            self._exc_text = traceback.format_exc()   # capture NOW; exc context is gone in _finish
        finally:
            self._finish()

    def _delivery_tick(self, frame, now):
        state = self._driver.classify(frame, self._ctx(now))
        if state in ("working", "quiet_working"):
            return                                   # CLI busy / not settled: keep waiting
        if state in ("crashed", "exited") or not self._handle.is_alive():
            self._task_delivery = "failed"            # loop exits -> finally -> _finish
            return
        if self._driver.is_accepting_input(frame):
            self._ensure_ask_mode()
            self._deliver_task()
        else:
            self._emit_blocked(frame)                # modal / onboarding / unknown

    def _deliver_task(self):
        if self._log is not None:
            self._log.debug("session", "delivery_attempt", session_id=self._id)
        # ONE delivery budget covers both the write and the confirmation wait, so a frozen
        # executor costs at most delivery_confirm_seconds (not 2x). A blocking PTY write
        # would otherwise wedge the monitor forever if the executor stops draining stdin.
        deadline = time.monotonic() + self._spec.delivery_confirm_seconds
        try:
            # drain_output: a CLI that echoes/re-renders a large paste fills the PTY output
            # buffer and then blocks writing it, which stops it reading our input. The monitor
            # owns both the write and the read here, so draining during the write breaks that
            # flow-control deadlock and lets a large task land. (respond()'s write stays
            # non-draining: it runs on the RPC thread, where draining would race pump().)
            self._type_text(self._task, timeout=max(0.0, deadline - time.monotonic()),
                            drain_output=True)
        except PtyWriteTimeout:
            self._fail_delivery("write_unconfirmed")   # executor not reading stdin
            return
        while time.monotonic() < deadline and not self._stop.is_set():
            self._handle.pump(0.1)
            with self._lock:
                frame = self._handle.render()
            if self._driver.input_submission_present(frame, self._task):
                self._press_enter()
                self._dialog.mark_turn_boundary()    # task turn begins now
                self._task_delivery = "delivered"
                self._last_state = None
                if self._log is not None:
                    self._log.audit_task(self._id, self._executor, self._task)
                    self._log.info("session", "delivery_confirmed", session_id=self._id)
                return
        # Not confirmed within the window (a slow paste should have shown by now): give up.
        self._fail_delivery("delivery_unconfirmed")

    def _fail_delivery(self, reason):
        # Give up cleanly: do NOT press Enter, do NOT re-type. Mark failed so the run loop
        # exits, and wake Hermes with a non-respondable advisory; the human stops + restarts.
        self._task_delivery = "failed"
        self._handle.flush_viewport(self._dialog)
        self._publish("delivery_failed", hint=reason, hung=False,
                      requires_response=False, task_delivery="failed")
        if self._log is not None:
            self._log.warning("session", "delivery_failed", session_id=self._id, reason=reason)

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
        # crashed/exited are terminal: _loop breaks and _finish() (run from _run's finally)
        # owns the single terminal event + state, status-mapped from leader_status().

    def _exit_kind(self, status):
        # deterministic from leader status (NOT driver classification)
        if status is None or status.signal is not None:
            return ("crashed", "crashed")
        if status.exit_code not in (0, None):
            return ("crashed", "crashed")
        if status.exit_code == 0:
            return ("done", "exited")
        return ("crashed", "crashed")                # dead but status unavailable

    def _finish(self):
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
        status = self._handle.leader_status() if self._handle is not None else None
        alive = bool(status and status.alive)
        # 1. monitor itself crashed; leader may still be alive
        if self._exc is not None:
            with self._lock:
                self._state = "crashed"
            self._publish("crashed", hint=None, hung=False, requires_response=False)
            if self._log is not None:
                self._log.error("session", "monitor_exception", session_id=self._id,
                                traceback=self._exc_text)
                if not alive:                          # executor_exited only if it actually exited
                    self._log_exited("monitor_exception", status)
                self._log.debug("session", "monitor_exited", session_id=self._id)
            return
        # 2. executor genuinely exited -> the ONE terminal exit event, status-mapped
        if not alive:
            kind, final_state = self._exit_kind(status)
            with self._lock:
                self._state = final_state
            self._publish("done" if kind == "done" else "crashed",
                          hint=None, hung=False, requires_response=False)
            self._log_exited(kind if kind == "done" else "crashed", status)
            if self._log is not None:
                self._log.debug("session", "monitor_exited", session_id=self._id)
            return
        # 3. operator stopped a LIVE agent -> manager logs session_stopped; no executor_exited
        if self._stop.is_set():
            if self._log is not None:
                self._log.debug("session", "monitor_exited", session_id=self._id)
            return
        # 4. delivery already failed AND surfaced -> no bogus crashed, no executor_exited
        if self._task_delivery == "failed":
            if self._log is not None:
                self._log.debug("session", "monitor_exited", session_id=self._id)
            return
        # 5. crash banner while leader is still alive
        with self._lock:
            self._state = "crashed"
        self._publish("crashed", hint=None, hung=False, requires_response=False)
        if self._log is not None:
            self._log.warning("session", "cli_crashed", session_id=self._id)
            self._log.debug("session", "monitor_exited", session_id=self._id)

    # ---- lifecycle logging helpers (one-liners over lifecycle_log; all logger-guarded) ----
    def _screen_fp(self):
        # a HASH of the normalized screen, never the screen content (spec §1b).
        if self._handle is None:
            return None
        norm = self._driver.normalize_frame(self._handle.render())
        return hashlib.sha256(norm.encode()).hexdigest()[:16]

    def _log_spawned(self, argv, launcher):
        if self._log is None:
            return
        lifecycle_log.log_executor_spawned(
            self._log, session_id=self._id, executor=self._executor,
            leader_pid=self._handle.leader_pid(), leader_pgid=self._handle.leader_pgid(),
            argv=argv, launcher=launcher)

    def _log_exited(self, reason, status):
        if self._log is None:
            return
        alive_for = (time.time() - self._spawn_ts) if self._spawn_ts else None
        lifecycle_log.log_executor_exited(
            self._log, session_id=self._id, reason=reason,
            leader_exit_code=(status.exit_code if status else None),
            leader_signal=(status.signal if status else None),
            status_available=(status.status_available if status else False),
            alive_for=alive_for, task_delivery=self._task_delivery,
            screen_fingerprint=self._screen_fp())

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
