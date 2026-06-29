import hashlib
import json
import re
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass

import paths
from daemon import lifecycle_log
from daemon import reaper
from daemon.dialog import Dialog
from daemon.transcript_builder import TranscriptBuilder
from daemon.drivers.base import ClassifyCtx
from daemon.events import EXTERNAL_OUTPUT_POLICY, RESPONDABLE_KINDS
from daemon.hygiene import prepare_pty_input
from daemon.errors import PtyWriteTimeout


@dataclass
class RespondOutcome:
    """Result of binding an answer to a session's current pending decision. `status` is one of
    'resumed' | 'no_pending' | 'stale' | 'unknown_session'; `pending` carries the current
    decision metadata when an optional decision_id guard mismatches (so the caller can reconcile)."""
    status: str
    seq: int = None
    decision_id: str = None
    pending: dict = None


def _pending_meta(decision):
    return {"decision_id": decision.get("decision_id"), "kind": decision["kind"],
            "requires_response": decision.get("requires_response", True),
            "hint": decision.get("hint"), "hung": decision.get("hung", False)}


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
        self._task = None              # held task (cleaned), delivered once a real input box appears
        self._task_raw = None          # original task text, for the human label / restart reuse
        self._cwd = None               # project dir, for the label / restart reuse / meta
        self.lineage_id = None         # manager-set: restart chain id (None until manager assigns)
        self.restarted_from = None     # manager-set: immediate predecessor session_id, or None
        self.restart_count = 0         # manager-set snapshot of the lineage count (display only)
        self._last_screen_excerpt = "" # last published screen excerpt (for the terminal snapshot)
        self._terminal_kind = None     # done | crashed | delivery_failed (set at each terminal point)
        self._task_delivery = "pending"  # pending | delivered | failed
        self._finalized = False        # _finish ran (idempotency guard, under self._lock)
        self._exc = None               # sys.exc_info() if the monitor body raised
        self._exc_text = None          # formatted traceback captured at catch time
        self._spawn_ts = None          # ts the PTY leader was spawned (for alive_for)
        self._blocked_fp = None        # normalized screen of the last emitted blocked event
        self._sessions_dir = _sessions_root()
        self.on_terminal = None        # manager-set: free the slot on terminal state
        self.reaper_ctx = None         # daemon-set reaper.ReaperContext (None => no reaping)
        self._closing = False          # terminal cleanup started: respond/screen must not write
        driver._settle = spec.settle_seconds   # keep classify a pure (frame, ctx) fn

    @property
    def dialog(self):
        return self._dialog

    @property
    def executor(self):
        return self._executor

    @property
    def task(self):
        return self._task_raw

    @property
    def cwd(self):
        return self._cwd

    # ---- lifecycle ----
    def start(self, task, cwd):
        # Non-blocking: spawn the PTY, hold the task, and let the monitor thread own
        # both delivery (deliver only into a verified input box) and the run loop.
        # /start returns once the PTY is spawned, NOT once the task is delivered.
        # Clean the held task FIRST (CLI-agnostic byte hygiene + the driver's command-prefix
        # policy): a rejected task raises before anything is spawned, and the cleaned text is
        # both what gets typed and what input_submission_present() matches against.
        self._task_raw = task          # keep the original for labels + restart reuse
        self._cwd = cwd
        self._task = prepare_pty_input(task, self._driver.command_prefixes)
        self._dialog = Dialog(self._sessions_dir / self._id,
                              tail_lines=self._spec.tail_lines,
                              spool_max_bytes=self._spec.spool_max_bytes)
        self._transcript = TranscriptBuilder(self._dialog, self._driver, self._rows)
        self._write_meta()
        self._handle = self._launcher.start(self._spec, cwd, self._cols, self._rows,
                                            dialog=self._dialog, transcript=self._transcript)
        self._task_delivery = "pending"
        self._spawn_ts = time.time()
        self._log_spawned(self._spec.argv(), type(self._launcher).__name__)
        self._record_child()
        if self._log is not None:
            self._log.debug("session", "monitor_started", session_id=self._id)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _write_meta(self):
        # Persist the dims (+ executor/driver) the raw is captured at, so nelix-capture can replay
        # sessions/<id>/raw at the right size. Private (0600) — same discipline as the raw. Best-effort:
        # never fail a session start over the capture sidecar.
        meta = {"cols": self._cols, "rows": self._rows, "executor": self._executor,
                "driver": getattr(self._spec, "driver", None),
                "task": self._task_raw, "cwd": self._cwd,
                "lineage_id": self.lineage_id, "restarted_from": self.restarted_from}
        try:
            with open(paths.session_meta(self._sessions_dir / self._id), "w",
                      opener=paths.private_opener) as f:
                json.dump(meta, f)
        except OSError:
            pass

    def _record_child(self):
        # Publish the reaping record AFTER spawn (pid/pgid known) and BEFORE the monitor
        # thread runs, so a crash from here on leaves a reapable record.
        ctx = self.reaper_ctx
        if ctx is None or self._handle is None:
            return
        # The reaper kills by process GROUP; that only reaps the whole subtree if the PTY
        # child is its own group leader (setsid -> pid == pgid). Enforce it before recording.
        self._handle.assert_leader_is_group_leader()
        pid, pgid = self._handle.leader_pid(), self._handle.leader_pgid()
        record = {"sid": self._id, "daemon_pid": ctx.daemon_pid,
                  "daemon_fingerprint": ctx.daemon_fingerprint, "pid": pid,
                  "child_fingerprint": ctx.inspector.start_fingerprint(pid),
                  "pgid": pgid, "argv": lifecycle_log.redact_argv(self._spec.argv())}
        try:
            reaper.record_child(self._sessions_dir / self._id, record)
            if self._log is not None:
                self._log.info("session", "child_recorded", session_id=self._id,
                               pid=pid, pgid=pgid)
        except OSError:
            if self._log is not None:
                self._log.warning("session", "child_record_failed", session_id=self._id)

    # ---- low-level PTY ops (split from the old blind _submit) ----
    def _type_text(self, text, timeout=None, drain_output=False):
        self._handle.write(text, timeout=timeout, drain_output=drain_output)

    def _press_enter(self, timeout=None):
        # Submit with the driver-declared submit key (CR for most TUIs, not LF).
        self._handle.write(self._driver.submit_key, timeout=timeout)

    def screen(self, raw=False):
        with self._lock:
            if self._closing:
                return ""
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
                                  requires_response=True, task_delivery="pending",
                                  decision_key=self._blocked_fp)   # same pause -> reuse decision_id
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
            # The driver frames the submission (the claude driver wraps it in a bracketed paste so
            # the CLI collapses it to a placeholder instead of re-rendering every char). The submit
            # key is pressed separately below, so it stays outside any paste frame.
            self._type_text(self._driver.format_submission(self._task),
                            timeout=max(0.0, deadline - time.monotonic()), drain_output=True)
        except PtyWriteTimeout:
            self._fail_delivery("write_unconfirmed")   # executor not reading stdin
            return
        while time.monotonic() < deadline and not self._stop.is_set():
            self._handle.pump(0.1)
            with self._lock:
                frame = self._handle.render()
            if self._driver.input_submission_present(frame, self._task):
                self._press_enter()
                self._dialog.append_user_input(self._task_raw)   # first user marker
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
        self._terminal_kind = "delivery_failed"
        self._handle.finalize()
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
        # dead but no waitpid status (broker-backed sessions, always): we cannot tell a
        # clean exit from a crash, so report a NEUTRAL terminal kind, not "crashed".
        return ("done", "exited")

    def _finish(self):
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
        status = self._handle.leader_status() if self._handle is not None else None
        alive = bool(status and status.alive)
        try:
            self._finish_publish(status, alive)
        finally:
            self._finish_cleanup(alive)

    def _finish_publish(self, status, alive):
        # 1. monitor itself crashed; leader may still be alive
        if self._exc is not None:
            with self._lock:
                self._state = "crashed"
                self._terminal_kind = "crashed"
            self._publish("crashed", hint=None, hung=False, requires_response=False)
            if self._log is not None:
                self._log.error("session", "monitor_exception", session_id=self._id,
                                traceback=self._exc_text)
                if not alive:                          # executor_exited only if it actually exited
                    self._log_exited("monitor_exception", status)
                self._log.debug("session", "monitor_exited", session_id=self._id)
            return
        # 2. delivery_failed already surfaced -> no bogus terminal event, no executor_exited.
        # Checked BEFORE the not-alive branch: if the child exits after _fail_delivery publishes,
        # that terminal kind is authoritative and must not be overwritten by the exit branch.
        # Distinct from _delivery_tick setting _task_delivery="failed" without surfacing the event
        # (child died mid-delivery): that path has _terminal_kind=None and falls through to branch 3.
        if self._terminal_kind == "delivery_failed":
            if self._log is not None:
                self._log.debug("session", "monitor_exited", session_id=self._id)
            return
        # 3. operator stopped the agent -> the ONE terminal 'stopped' event. Checked BEFORE the
        # not-alive branch because Session.stop() kills the launcher first, so the monitor reaches
        # _finish with alive=False; without this precedence the kill would be mis-reported as
        # done/crashed. The monitor runs _finish exactly once -> exactly one terminal event, so the
        # per-session waiter fires and exits. (Accepted race: a natural exit immediately followed by
        # a stop request reports 'stopped' — the session is terminal either way.)
        if self._stop.is_set():
            with self._lock:
                self._state = "stopped"
                self._terminal_kind = "stopped"
            self._publish("stopped", hint=None, hung=False, requires_response=False)
            if self._log is not None:
                self._log.debug("session", "monitor_exited", session_id=self._id)
            return
        # 4. executor genuinely exited -> the ONE terminal exit event, status-mapped
        if not alive:
            kind, final_state = self._exit_kind(status)
            with self._lock:
                self._state = final_state
                self._terminal_kind = "done" if kind == "done" else "crashed"
            self._publish("done" if kind == "done" else "crashed",
                          hint=None, hung=False, requires_response=False)
            self._log_exited(kind if kind == "done" else "crashed", status)
            if self._log is not None:
                self._log.debug("session", "monitor_exited", session_id=self._id)
            return
        # 5. crash banner while leader is still alive
        with self._lock:
            self._state = "crashed"
            self._terminal_kind = "crashed"
        self._publish("crashed", hint=None, hung=False, requires_response=False)
        if self._log is not None:
            self._log.warning("session", "cli_crashed", session_id=self._id)
            self._log.debug("session", "monitor_exited", session_id=self._id)

    def _finish_cleanup(self, alive):
        # Terminal cleanup for ANY exit reason: reap survivors (monitor-dead-child-alive, or
        # stragglers in the group), forget the durable record, free the concurrency slot.
        with self._lock:
            self._closing = True
        ctx = self.reaper_ctx
        if ctx is not None and self._handle is not None:
            pid, pgid = self._handle.leader_pid(), self._handle.leader_pgid()
            if alive and pid is not None and pgid is not None:
                reaper.kill_group(ctx.inspector, ctx.killer, pid, pgid, ctx.grace)
            reaper.forget_child(self._sessions_dir / self._id)
        cb = self.on_terminal
        if cb is not None:
            try:
                cb(self._id)
            except Exception:
                if self._log is not None:
                    self._log.error("session", "on_terminal_error", session_id=self._id,
                                    exc_info=True)

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
        self._handle.finalize()
        hint = "needs_permission" if state == "permission_prompt" else None
        # decision_key identifies the pause by what is ON SCREEN, not just the classifier label:
        # the same stalled/idle screen reuses its decision_id (a hung re-emit), a different screen
        # (a genuinely different question) starts a new decision. Mirrors blocked's fingerprint key.
        fp = self._driver.normalize_frame(self._handle.render()) if self._handle is not None else ""
        self._publish("waiting_for_user", hint=hint, hung=hung, requires_response=True,
                      decision_key=f"stop:{state}:{fp}")

    def _emit_blocked(self, frame, hint="task_not_delivered"):
        # Surface a pre-delivery interstitial (modal / onboarding / unknown). Type/press NOTHING.
        # Dedup by normalized-screen fingerprint alone: emit once per distinct screen. A new
        # interstitial (different fingerprint) emits a fresh blocked; the same screen never re-spams,
        # including after the prior one is answered (the answer changes the screen anyway).
        fp = self._driver.normalize_frame(frame)
        if fp == self._blocked_fp:
            return
        self._blocked_fp = fp
        self._handle.finalize()
        # decision_key = the normalized-frame fingerprint: a new interstitial (different fp) is a
        # new decision; the no-progress backstop re-emits the SAME fp and reuses the decision_id.
        self._publish("blocked", hint=hint, hung=False, requires_response=True,
                      task_delivery="pending", decision_key=fp)

    def _publish(self, kind, hint, hung, requires_response=None, task_delivery=None,
                 decision_key=None):
        respondable = kind in RESPONDABLE_KINDS
        with self._lock:
            tail = self._dialog.tail(self._spec.status_tail_chars)
            text = tail["text"]
            truncated = tail["start_offset"] > 0  # tail didn't start from the beginning
            # render() directly (NOT self.screen(), which also takes self._lock -> deadlock).
            screen = _excerpt(self._handle.render() if self._handle is not None else "",
                              self._spec.status_tail_chars)
            self._last_screen_excerpt = screen
            if requires_response is None:
                requires_response = respondable
            if task_delivery is None:
                task_delivery = self._task_delivery
            decision = {"kind": kind, "hint": hint, "hung": hung, "text": text,
                        "task_delivery": task_delivery, "requires_response": requires_response,
                        "screen_excerpt": screen, "external_output_policy": EXTERNAL_OUTPUT_POLICY,
                        "total_len": tail["total_len"], "truncated": truncated,
                        "last_user_input_offset": self._dialog.last_user_input_offset()}
            is_reemit = False
            if respondable:
                # decision identity: REUSE the current decision's id when this is a re-emit of the
                # same pause (same decision_key), else mint a fresh one. event_id (below) is the
                # NOTIFICATION identity and changes every emit; decision_id is stable across re-emits
                # so a held answer never self-invalidates.
                cur = self._decision
                is_reemit = cur is not None and cur.get("decision_key") == decision_key
                decision_id = cur["decision_id"] if is_reemit else f"dec-{uuid.uuid4().hex[:8]}"
                decision["decision_key"] = decision_key
                decision["decision_id"] = decision_id

        def _install(evt):
            # Runs under the EventQueue lock, AFTER the event is reserved but BEFORE waiters are
            # notified: a woken status pull therefore always observes the installed decision (no
            # event-without-decision window). The branches are race-safe against a concurrent
            # respond() that may have CLAIMED the decision between build and publish:
            with self._lock:
                cur = self._decision
                if cur is not None and cur.get("decision_id") == decision_id:
                    # same logical decision still pending -> refresh notification identity + hung
                    # IN PLACE (never swap the object: keeps respond()'s claim identity stable).
                    cur["event_id"] = evt.event_id
                    cur["seq"] = evt.seq
                    cur["hung"] = hung
                elif not is_reemit:
                    # a genuinely new pause -> install it (supersedes any prior pending decision).
                    decision["event_id"] = evt.event_id
                    decision["seq"] = evt.seq
                    self._decision = decision
                else:
                    # a re-emit whose decision was answered/superseded between build and publish:
                    # obsolete -> mark the event answered so pending() never resurrects it.
                    evt.answered = True

        # publish OUTSIDE the session lock (lock order: never hold it across a queue publish).
        evt = self._events.publish(self._id, self._executor, kind, text[:200], self._state,
                                   hint=hint, hung=hung, task_delivery=task_delivery,
                                   requires_response=requires_response, screen_excerpt=screen,
                                   on_publish=_install if respondable else None)
        if self._log is not None:
            self._log.audit_decision(self._id, self._executor, kind, evt.event_id, text)

    # ---- reads / control ----
    def is_working(self):
        with self._lock:
            return self._decision is None and self._state in ("working", "quiet_working")

    def snapshot(self):
        with self._lock:
            snap = {"session_id": self._id, "executor": self._executor,
                    "task": self._task_raw, "cwd": self._cwd,
                    "state": self._state}
            if self.lineage_id is not None:
                snap["lineage_id"] = self.lineage_id
                snap["restarted_from"] = self.restarted_from
                snap["restart_count"] = self.restart_count
            if self._decision is not None:
                # decision_key is internal identity (never exposed); decision_id is the public
                # guard token.  The text was captured at publish time via tail() and is frozen.
                dec = {k: v for k, v in self._decision.items() if k != "decision_key"}
                snap["decision"] = dec
            # active-working snapshots are deliberately low-information: no progress bait, just
            # "end your turn" — nelix wakes Hermes on the next event, so there is nothing to poll.
            snap["pending"] = self._decision is not None
            if self._decision is None and self._state in ("working", "quiet_working"):
                snap["message"] = ("Agent is still working. End your turn; nelix will wake "
                                   "you on the next event.")
            return snap

    def terminal_snapshot(self):
        """Read-only advisory snapshot for a disappearing session, so the companion can relay
        a completion/crash even after the manager has freed the slot. Display-only; the durable
        restart count lives in the manager's lineage table."""
        with self._lock:
            return {"session_id": self._id, "executor": self._executor,
                    "task": self._task_raw, "cwd": self._cwd, "state": self._state,
                    "terminal_kind": self._terminal_kind,
                    "screen_excerpt": self._last_screen_excerpt,
                    "lineage_id": self.lineage_id, "restarted_from": self.restarted_from,
                    "restart_count": self.restart_count, "terminal": True}

    def respond(self, answer, decision_id=None):
        # Bind to the session's CURRENT pending decision (server owns identity). decision_id is an
        # OPTIONAL staleness guard sourced from the status pull, never required from the wake.
        with self._lock:
            if self._closing:
                return RespondOutcome("terminal")
            decision = self._decision
            if decision is None or "event_id" not in decision:
                return RespondOutcome("no_pending")
            if decision_id is not None and decision.get("decision_id") != decision_id:
                return RespondOutcome("stale", pending=_pending_meta(decision))
        # Clean the answer BEFORE claiming: a rejected answer (command prefix / empty after
        # sanitization) leaves the decision pending and nothing typed, so the caller can retry.
        clean = prepare_pty_input(answer, self._driver.command_prefixes)
        # Atomically CLAIM the decision: exactly one responder clears it and goes on to type, so
        # concurrent duplicate responds can never both write to the PTY (which is non-idempotent).
        with self._lock:
            if self._decision is not decision:
                return RespondOutcome("no_pending")    # already claimed, or superseded by a new pause
            self._decision = None
        is_blocked = decision["kind"] == "blocked"
        # one logical decision may span several notification events (backstop re-emits) -> answer all,
        # so pending() stays honest and the next waiter arms past the whole resolved decision.
        seq = self._events.mark_session_answered(self._id) or decision.get("seq")
        if self._handle is not None:
            # Bound the PTY write (this runs on the RPC thread): a wedged executor that stopped
            # draining its stdin must NOT hang respond forever. ONE deadline covers the answer text +
            # the submit key; on timeout the answer did not land (executor wedged) -> report it, don't
            # re-type. Non-draining (the monitor owns pump(); draining here would race it).
            deadline = time.monotonic() + self._spec.respond_write_seconds
            try:
                self._type_text(clean, timeout=max(0.0, deadline - time.monotonic()))
                self._press_enter(timeout=max(0.0, deadline - time.monotonic()))
            except PtyWriteTimeout:
                if self._log is not None:
                    self._log.warning("session", "respond_write_timeout", session_id=self._id)
                return RespondOutcome("write_timeout", decision_id=decision.get("decision_id"))
            if not is_blocked:
                # Only a delivered-agent respond appends a user marker; a write_timeout must not
                # advance the transcript. (The monitor reads dialog in _publish under self._lock.)
                with self._lock:
                    self._dialog.append_user_input(clean)
            self._last_state = None
        return RespondOutcome("resumed", seq=seq, decision_id=decision.get("decision_id"))

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
