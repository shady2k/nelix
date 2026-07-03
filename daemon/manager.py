import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, replace

import paths
from daemon.drivers import get_driver
from daemon.env_resolver import EnvResolveError, resolve_env_cmds, _run_capture
from daemon.events import EXTERNAL_OUTPUT_POLICY
from daemon.session import RespondOutcome, Session

_MODEL_MAX_LEN = 128     # sane shape cap; nelix keeps NO model allowlist (the CLI is the authority)
_MODELS_MAX_BYTES = 65536  # nelix-g9k: bounded-capture cap for models_cmd stdout (a model list is
                           # small; a producer past this is misconfigured -> output_too_large -> 502)


class ModelRejected(ValueError):
    """A per-session `model` override that nelix refuses BEFORE spawning: a bad-shape value
    (empty / control chars / oversized) or a driver that cannot express a model override. A
    ValueError subclass (like PtyInputRejected) so /start maps it to 400 — caught ahead of the
    generic ValueError->409 branch (client input error, not daemon-full)."""


class ModelsNotConfigured(Exception):
    """nelix-g9k: the executor has no `models_cmd` configured. A distinct type (NOT a ValueError)
    so the /models route maps it to a clean 400 'not configured' — relayable, so the orchestrator
    learns not to retry — separate from an unknown-executor ValueError (404)."""

    def __init__(self, executor):
        super().__init__(f"executor {executor!r} has no models_cmd configured")
        self.executor = executor


class ModelsCmdError(Exception):
    """nelix-g9k: `models_cmd` failed to produce output. Carries ONLY `reason` (∈ the _run_capture
    reason set) — never the command, stdout, or stderr — so the route/manager can log/relay {reason}
    without leaking a secret the command may have referenced (spec §5). Maps to a redacted 502."""

    def __init__(self, reason):
        super().__init__(f"models_cmd failed: {reason}")
        self.reason = reason


def _validate_model_shape(model):
    """Shape-only validation (spec §5): pass-through — nelix keeps no allowlist, the CLI is the
    authority on model validity. A clean value is forwarded VERBATIM; the checks run on the ORIGINAL
    string (never a normalized copy) so an edge control char or surrounding whitespace is REJECTED,
    not silently trimmed-and-accepted. `.strip()` is used ONLY to detect the empty/whitespace-only
    case. Returns the value unchanged, or raises ModelRejected."""
    if not isinstance(model, str):
        raise ModelRejected(f"model must be a string, got {type(model).__name__}")
    if not model.strip():
        raise ModelRejected("model is empty or whitespace-only")
    if len(model) > _MODEL_MAX_LEN:
        raise ModelRejected(f"model is too long (max {_MODEL_MAX_LEN} chars)")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in model):
        raise ModelRejected("model contains ASCII control characters (incl newline/tab)")
    if model != model.strip():
        raise ModelRejected("model has leading or trailing whitespace")
    return model


def _strip_model_flag(args, flag):
    """Remove any existing occurrence of `flag` in BOTH `<flag> <v>` and `<flag>=<v>` forms (the
    fold is driver-flag-based, never a globally-hardcoded '--model'). Returns (cleaned, stripped)."""
    cleaned, stripped, i, n = [], False, 0, len(args)
    eq = flag + "="
    while i < n:
        a = args[i]
        if a == flag:
            stripped = True
            i += 2 if i + 1 < n else 1      # drop the flag AND its value (or a malformed trailing flag)
            continue
        if a.startswith(eq):
            stripped = True
            i += 1
            continue
        cleaned.append(a)
        i += 1
    return cleaned, stripped


@dataclass
class StartOutcome:
    session_id: str
    base_seq: int
    snapshot: dict = None


@dataclass
class StopOutcome:
    status: str                    # 'stopped' | 'stop_requested' | 'unknown_session'
    snapshot: dict = None


@dataclass
class RestartOutcome:
    status: str                    # 'restarted' | 'unknown_session' | 'restart_budget_exhausted' | 'start_failed'
    session_id: str = None
    lineage_id: str = None
    restart_count: int = None
    max_restarts: int = None
    next_after_seq: int = None
    snapshot: dict = None


def _session_activity(d):
    """Last-activity ts: transcript.jsonl mtime, else newest file mtime, else dir mtime.
    Dir mtime alone doesn't move when files inside are written, so a live session could
    look stale by it — never use it as the safety rule (registered-id exclusion is)."""
    tj = d / "transcript.jsonl"
    try:
        if tj.exists():
            return tj.stat().st_mtime
        mtimes = [f.stat().st_mtime for f in d.iterdir() if f.is_file()]
        return max(mtimes) if mtimes else d.stat().st_mtime
    except OSError:
        return 0.0


def _rmtree(d, logger):
    try:
        shutil.rmtree(d)
    except OSError as e:
        if logger is not None:
            logger.warning("manager", "session_gc_skip", dir=str(d), err=str(e))


def gc_sessions(keep_ids, retain, max_age_days, now=None, logger=None):
    """Prune inactive session dirs by age then count. NEVER touches a dir whose name is
    in keep_ids (registered/active) — exclusion-before-delete is the only safety rule.
    retain/max_age_days of 0 disable that rake. Best-effort."""
    now = time.time() if now is None else now
    root = paths.sessions_root()
    try:
        dirs = [d for d in root.iterdir() if d.is_dir() and d.name not in keep_ids]
    except FileNotFoundError:
        return
    survivors = []
    for d in dirs:
        if max_age_days and (now - _session_activity(d)) / 86400.0 > max_age_days:
            _rmtree(d, logger)
        else:
            survivors.append(d)
    if retain and len(survivors) > retain:
        survivors.sort(key=_session_activity)              # oldest first
        for d in survivors[:len(survivors) - retain]:
            _rmtree(d, logger)


def _default_session_factory(sid, executor, spec, events, launcher_factory,
                             driver_factory, logger):
    return Session(sid, executor, driver_factory(spec.driver),
                   launcher_factory(spec.launcher), spec, events, logger=logger)


class SessionManager:
    """Registry of sessions. Holds <= concurrency_limit (config-driven, default 5)."""

    def __init__(self, specs, events, launcher_factory=None, driver_factory=None,
                 concurrency_limit=5, idle_retained_limit=None, logger=None, session_factory=None,
                 session_retain=20, session_max_age_days=7, reaper_ctx=None,
                 terminal_snapshot_ttl=300.0, clock=time.time):
        self._specs = specs
        self._events = events
        self._limit = concurrency_limit
        # An `idle` session frees its active slot but is retained alive; this bounds how many such
        # completed-but-unclosed sessions we keep. Defaults to the active concurrency limit.
        self._idle_limit = idle_retained_limit if idle_retained_limit is not None else concurrency_limit
        self._logger = logger
        self._session_retain = session_retain
        self._session_max_age_days = session_max_age_days
        self._reaper_ctx = reaper_ctx
        self._sessions = {}
        self._lineages = {}            # lineage_id -> restart count (durable across session removal)
        self._reserved = 0             # in-flight restart slot reservations (cap accounting)
        self._terminal = {}            # sid -> (snapshot_dict, expires_at): disappeared-session relay
        # sid -> ({"id":..., "reason":"executor_finished"}, expires_at): Task 6 terminal survival for
        # an async question still outstanding when the executor exits. Written ALONGSIDE self._terminal
        # in _free_slot with the SAME expires_at (one lifetime policy, not two) — never write this
        # without also having just computed self._terminal's expiry for the same session.
        self._terminal_async = {}
        self._terminal_ttl = terminal_snapshot_ttl
        self._clock = clock
        # Same seam _make uses for session construction, reused for the model-capability read (a
        # model override reads driver.model_flag). Never call get_driver() directly — that would
        # bypass an injected factory (tests / custom drivers).
        self._driver_factory = driver_factory or get_driver
        self._lock = threading.Lock()
        if session_factory is not None:
            self._make = lambda sid, ex, spec: session_factory(sid, ex, spec, events)
        else:
            self._make = lambda sid, ex, spec: _default_session_factory(
                sid, ex, spec, events, launcher_factory, driver_factory, logger)

    def start(self, executor_name, task, cwd, model=None):
        return self._spawn(executor_name, task, cwd, lineage_id=None, restarted_from=None,
                           model=model)

    def _apply_model_override(self, spec, executor_name, model):
        """Validate + fold a per-session `model` into a fresh per-session ExecutorSpec (last-wins).
        Runs BEFORE the session lock and cap checks so a bad/unsupported model returns 400, not a
        409 'daemon full'. Idempotent — re-applying the already-validated value (the restart/recovery
        path does exactly this against the fresh original spec) re-strips + re-folds to the same argv.
        Returns (folded_spec, validated_model). Raises ModelRejected on bad shape or an incapable driver."""
        model = _validate_model_shape(model)
        flag = getattr(self._driver_factory(spec.driver), "model_flag", None)
        if flag is None:
            raise ModelRejected(
                f"executor {executor_name!r} (driver {spec.driver!r}) does not support a model override")
        cleaned, stripped = _strip_model_flag(spec.args, flag)
        if stripped and self._logger is not None:
            # A toml-pinned model was overridden — an operationally significant, otherwise-silent action.
            self._logger.info("manager", "model_override_applied", executor=executor_name,
                              model=model, driver=spec.driver)
        return replace(spec, args=[*cleaned, flag, model]), model

    def _log_spawn_failure(self, event, session_id, exc):
        """Log a spawn/restart failure. An EnvResolveError (nelix-c5o) is logged REDACTED — only
        {var, reason}, WITHOUT exc_info: the exception is raised `from None` and stores no command /
        stdout / stderr (so even a traceback could not leak the secret; spec §5), and a structured
        record is cleaner. Every other error keeps the exc_info traceback for diagnosis."""
        if self._logger is None:
            return
        if isinstance(exc, EnvResolveError):
            self._logger.error("manager", event, session_id=session_id,
                               reason="env_resolve_failed", var=exc.var, resolve_reason=exc.reason)
        else:
            self._logger.error("manager", event, session_id=session_id, exc_info=True)

    def _spawn(self, executor_name, task, cwd, *, lineage_id, restarted_from, reserve=False,
               model=None):
        # reserve=True: a slot reservation was made for us by restart() (old session popped +
        # self._reserved bumped under the lock). We OWN that reservation and must release it exactly
        # once: consume it ATOMICALLY with inserting the new session (so len(_sessions)+_reserved
        # never overcounts), or release it in `finally` if we raise before inserting.
        consumed = not reserve
        try:
            spec = self._specs.get(executor_name)
            if spec is None:
                if self._logger is not None:
                    self._logger.warning("manager", "session_start_rejected",
                                         reason="unknown_executor", executor=executor_name)
                raise RuntimeError(f"unknown executor: {executor_name!r} "
                                   f"(configured: {sorted(self._specs)})")
            cwd = os.path.abspath(os.path.expanduser(cwd))
            if not os.path.isdir(cwd):          # host-side: fail fast, no session, no auto-mkdir
                if self._logger is not None:
                    self._logger.warning("manager", "session_start_rejected",
                                         reason="bad_cwd", executor=executor_name)
                raise ValueError(f"cwd does not exist or is not a directory: {cwd!r}")
            # Per-session model override (nelix-9k0): validate + fold BEFORE the lock/cap checks so a
            # bad-shape or unsupported-driver model returns 400, never a 409 "daemon full". Omitted
            # model -> spec is untouched (byte-identical to pre-feature). The validated value is stored
            # on the session (+ meta) so an auto-restart re-injects the SAME model, not the default.
            applied_model = None
            if model is not None:
                spec, applied_model = self._apply_model_override(spec, executor_name, model)
            with self._lock:
                # Split accounting (spec §slots): the active cap counts only sessions occupying an
                # active slot (everything except `idle`) + in-flight restart reservations; a retained
                # `idle` session frees its active slot but is bounded by idle_retained_limit. A restart
                # (reserve=True) reuses its own net-zero slot and skips both caps.
                if not reserve and self._active_count() + self._reserved >= self._limit:
                    if self._logger is not None:
                        self._logger.warning("manager", "session_start_rejected",
                                             reason="concurrency_limit", executor=executor_name)
                    raise RuntimeError(
                        f"concurrency_limit={self._limit} reached "
                        f"(active: {sorted(self._sessions)})")
                if not reserve and self._idle_count() >= self._idle_limit:
                    if self._logger is not None:
                        self._logger.warning("manager", "session_start_rejected",
                                             reason="idle_retained_limit", executor=executor_name)
                    raise RuntimeError(
                        f"idle_retained_limit={self._idle_limit} reached "
                        f"(close a completed session with nelix_stop before starting more)")
                sid = f"s-{uuid.uuid4().hex[:8]}"
                base_seq = self._events.latest_seq()  # waiter arms past anything already emitted
                sess = self._make(sid, executor_name, spec)
                sess.on_terminal = self._free_slot
                # Task 4: the monitor delivers a queued async reply as a fresh turn but has no manager
                # handle of its own — give it one that re-acquires an active slot (send_turn), so the
                # slot accounting an idle-freed session needs is preserved on the monitor-driven write.
                sess.deliver_turn = lambda text, _sid=sid: self.send_turn(_sid, text)
                sess.reaper_ctx = self._reaper_ctx
                sess.lineage_id = lineage_id or sid          # first in chain -> lineage = own id
                sess.restarted_from = restarted_from
                sess.restart_count = self._lineages.get(sess.lineage_id, 0)
                sess.model = applied_model                   # validated override (or None): survives restart
                self._sessions[sid] = sess
                if reserve:
                    self._reserved -= 1                      # consume atomically with the insert
                    consumed = True
                keep = set(self._sessions)
        finally:
            if not consumed:
                with self._lock:
                    self._reserved -= 1                      # raised before insert: release the reservation
        if self._logger is not None:
            self._logger.info("manager", "session_created", session_id=sid,
                              executor=executor_name, cwd=cwd,
                              lineage_id=sess.lineage_id, restarted_from=restarted_from,
                              slot=f"{len(keep)}/{self._limit}")
        gc_sessions(keep, self._session_retain, self._session_max_age_days, logger=self._logger)
        try:
            sess.start(task, cwd)
        except Exception as e:
            try:
                sess.stop()                       # tear down any partially-spawned PTY / open dialog
            except Exception:
                pass
            with self._lock:                      # don't leak a registered-but-unstarted session
                self._sessions.pop(sid, None)     # reservation already consumed: slot frees cleanly
            self._log_spawn_failure("session_start_failed", sid, e)
            raise
        return StartOutcome(session_id=sid, base_seq=base_seq, snapshot=sess.snapshot())

    def _restart_source(self, session_id):
        """Resolve (executor, task, cwd, lineage_id, active_session_or_None, model) for a restart.
        Source is an ACTIVE session if present, else the PERSISTED session-dir meta (the main path:
        a crashed/done session has already been removed from _sessions). `model` is the per-session
        override to re-apply (or None); OLD meta lacking the field defaults to None (no override, the
        pre-nelix-9k0 behaviour). Returns None if neither source exists."""
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is not None:
            return (sess.executor, sess.task, sess.cwd,
                    sess.lineage_id or session_id, sess, getattr(sess, "model", None))
        try:
            meta = json.loads(paths.session_meta(paths.sessions_root() / session_id).read_text())
        except (OSError, ValueError):
            return None
        if not meta.get("executor") or meta.get("cwd") is None:
            return None
        return (meta["executor"], meta.get("task"), meta["cwd"],
                meta.get("lineage_id") or session_id, None, meta.get("model"))

    def restart(self, session_id, force=False):
        src = self._restart_source(session_id)
        if src is None:
            return RestartOutcome("unknown_session")
        executor, task, cwd, lineage_id, active, model = src
        spec = self._specs.get(executor)
        max_restarts = spec.max_restarts if spec is not None else 0
        with self._lock:
            count = self._lineages.get(lineage_id, 0)
            if not force and count >= max_restarts:
                return RestartOutcome("restart_budget_exhausted", lineage_id=lineage_id,
                                      restart_count=count, max_restarts=max_restarts)
            if force:
                count = 0
            count += 1
            self._lineages[lineage_id] = count
            # RE-VALIDATE liveness UNDER the lock: the session resolved as active may have exited
            # between _restart_source and here (its monitor's _free_slot popped it). Only take the
            # net-zero reserve path if it is STILL in _sessions now; otherwise its slot was already
            # freed -> compete for a slot like a fresh start (normal cap check), so we can't bypass
            # the cap on a session that another start has since replaced.
            still_active = session_id in self._sessions
            if still_active:
                self._sessions.pop(session_id, None)
                self._reserved += 1
            reserve = still_active
        if still_active:
            try:
                active.stop()
            except Exception:
                if self._logger is not None:
                    self._logger.warning("manager", "restart_stop_error",
                                         session_id=session_id, exc_info=True)
        # _spawn OWNS the reservation (reserve=reserve): it consumes it atomically with the insert,
        # or releases it in its own finally if it raises before inserting. restart() must NOT also
        # touch self._reserved here (that would double-decrement).
        try:
            # Re-apply the per-session model override (same-lineage recovery must not silently drop
            # it). _spawn re-validates + re-folds against the fresh original spec (idempotent).
            started = self._spawn(executor, task, cwd, lineage_id=lineage_id,
                                  restarted_from=session_id, reserve=reserve, model=model)
            new_sid, base_seq = started.session_id, started.base_seq
        except Exception as e:
            self._log_spawn_failure("restart_spawn_failed", session_id, e)
            return RestartOutcome("start_failed", lineage_id=lineage_id,
                                  restart_count=count, max_restarts=max_restarts)
        if self._logger is not None:
            self._logger.info("manager", "session_restarted", session_id=new_sid,
                              restarted_from=session_id, lineage_id=lineage_id,
                              restart_count=count)
        return RestartOutcome("restarted", session_id=new_sid, lineage_id=lineage_id,
                              restart_count=count, max_restarts=max_restarts,
                              next_after_seq=base_seq, snapshot=started.snapshot)

    def _active_count(self):
        # MUST hold self._lock. Sessions occupying an ACTIVE concurrency slot: every live session
        # EXCEPT an `idle` one (turn complete, alive, awaiting a follow-up — it holds a PTY but not
        # an active slot). busy / awaiting_user / intervention_required / starting all still count:
        # the rule is exclude-idle, NOT a positive busy-only allowlist (which would wrongly free the
        # slot for a stuck/blocked/starting session that still owns a real process).
        return sum(1 for s in self._sessions.values()
                   if s.snapshot().get("control_state") != "idle")

    def _idle_count(self):
        # MUST hold self._lock. Retained `idle` sessions (completed, alive), bounded by idle_retained_limit.
        return sum(1 for s in self._sessions.values()
                   if s.snapshot().get("control_state") == "idle")

    def _free_slot(self, session_id):
        with self._lock:
            sess = self._sessions.get(session_id)
            snap = None
            qid = None
            if sess is not None:
                try:
                    snap = sess.terminal_snapshot()
                except Exception:
                    snap = None
                try:
                    qid = sess.pending_async_id()
                except Exception:
                    qid = None
                if qid is not None:
                    # Terminal survival (Task 6): an async question was still outstanding when the
                    # executor exited. Auto-resolve it now — CORRELATION only (mark the async event
                    # answered + clear the slot). resolve_async_question is closing-aware (session._finish
                    # already set _closing before on_terminal ever fires) so this always returns
                    # not_delivered and types NOTHING — the executor is gone, there is no PTY to write to.
                    try:
                        sess.resolve_async_question(qid, None)
                    except Exception:
                        pass
            # a CLEAN terminal (terminal_kind="done") ends the lineage; a crash/stop keeps it for a
            # possible restart. Keyed on terminal_kind, not a state string (NIT-16).
            if snap is not None and snap.get("terminal_kind") == "done":
                self._lineages.pop(snap.get("lineage_id"), None)
            existed = self._sessions.pop(session_id, None) is not None
            if self._terminal_ttl > 0:
                # ONE expiry computed here, reused for BOTH stores below: the recent-terminal async
                # record must share self._terminal's exact retention policy, never a second one.
                expires_at = self._clock() + self._terminal_ttl
                if snap is not None:
                    self._terminal[session_id] = (snap, expires_at)
                if qid is not None:
                    self._terminal_async[session_id] = (
                        {"id": qid, "reason": "executor_finished"}, expires_at)
        if existed and self._logger is not None:
            self._logger.info("manager", "slot_freed", session_id=session_id)

    def get(self, session_id):
        with self._lock:
            return self._sessions.get(session_id)

    def record_async_question(self, session_id, q):
        """Manager-level entry for the message-plane `question` route (Task 5): look up the LIVE
        session and delegate to Session.record_async_question. An absent/already-freed session
        returns the same unknown_session-equivalent shape as the in-Session already-pending error
        ((None, {"error": ...})), so the route can map either failure to an HTTP error the same way."""
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is None:
            return None, {"error": "unknown_session"}
        return sess.record_async_question(q)

    def append_progress_note(self, session_id, note):
        """Manager-level entry for the message-plane `note` route (Task 5): look up the LIVE session
        and delegate to Session.append_progress_note. An absent/already-freed session returns None
        (no progress_seq to report)."""
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is None:
            return None
        return sess.append_progress_note(note)

    def screen(self, session_id, raw=False, force=False):
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is None:
            return {"error": "unknown session"}
        # While the agent is actively working, withhold the screen (poll bait) unless explicitly
        # forced — the wake's screen_excerpt is the ground truth between events. `raw` only selects
        # cleaned-vs-raw formatting; it must NOT be an escape hatch around withholding (only force is).
        if sess.is_working() and not force:
            return {"control_state": "busy", "pending": False,
                    "message": ("Agent is still working. End your turn; nelix will wake you on the "
                                "next event. Pass force:true to see the screen anyway.")}
        # the external-output trust fence rides WITH the captured screen content (not the doorbell).
        return {"screen": sess.screen(raw=raw), "cols": sess._cols, "rows": sess._rows,
                "external_output_policy": EXTERNAL_OUTPUT_POLICY}

    def respond(self, session_id, answer, decision_id=None):
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                # Terminal survival (Task 6): the session already exited (its slot freed by
                # _free_slot) but it had an OUTSTANDING async question when it did — terminal cleanup
                # auto-resolved that into self._terminal_async (same key + expiry as self._terminal).
                # A caller whose decision_id names THAT question gets a clean
                # not_delivered/executor_finished, never a bare unknown_session (which reads like a
                # typo'd/unrelated session id rather than "your question's answer arrived too late").
                entry = self._terminal_async.get(session_id)
                if (entry is not None and decision_id is not None
                        and entry[1] > self._clock() and entry[0].get("id") == decision_id):
                    return RespondOutcome("not_delivered", reason=entry[0].get("reason"))
                return RespondOutcome("unknown_session")
        # Async-reply id-dispatch (Task 4): a decision_id that names an OUTSTANDING ASYNC QUESTION (not
        # a blocking decision) is answered by delivering a FRESH user turn, NOT by typing into a modal
        # (the executor never paused — there is no modal). Correlation (mark_answered + clear the slot)
        # and delivery (the fresh-turn write) are separate: the session resolves + decides disposition,
        # the manager owns the slot-reacquiring write. This is checked BEFORE the idle branch because a
        # session can be idle AND hold a pending async question at once (asked, then finished the turn).
        if decision_id and sess.has_pending_async(decision_id):
            disposition, text = sess.resolve_async_question(decision_id, answer)
            if disposition == "deliver_now":
                out = self.send_turn(session_id, text)       # idle now -> re-acquire slot + fresh turn
                # resolve_async_question already cleared the slot + marked the event answered, so if
                # send_turn DECLINES WITHOUT typing (at_capacity: other sessions saturate the limit;
                # no_pending: the state flipped busy in this RPC->monitor window) the framed reply would
                # be lost and an orchestrator retry would deliver a BARE answer. Re-queue it (nothing
                # typed here) so the monitor re-delivers the full frame at the next idle — symmetric
                # with drain_async_reply's re-queue.
                if out.status in ("at_capacity", "no_pending"):
                    sess.requeue_async_reply(text)
                return out
            if disposition == "queued_busy":
                # busy -> the monitor delivers at the next idle (drain_async_reply). Nothing typed yet.
                return RespondOutcome("queued", snapshot=sess.snapshot())
            return RespondOutcome("not_delivered", snapshot=sess.snapshot())  # closing/terminal
        # A follow-up on an IDLE session (turn complete, alive, no respondable decision) is a NEW
        # turn: route it through send_turn (re-acquire an active slot + re-open the turn) — never
        # respond(), whose no_pending path can't drive a non-respondable idle decision (plan Task 10).
        #
        # M4 (final whole-branch review, doc-only): this branch is reached — instead of the
        # decision_id-dispatch branch above — whenever `decision_id` is falsy or names no
        # outstanding async question, INCLUDING the case where the session is idle with a lone
        # outstanding async_question and the caller answered it WITHOUT a decision_id. Async answers
        # SHOULD pass decision_id (see the `question` tool
        # docs), but this is a deliberate choice, not an oversight: a strict guard here (rejecting a
        # decision_id-less idle answer) would also break legitimate idle follow-ups, which never
        # carry a decision_id. So a decision_id-less answer on an idle session is treated as a plain
        # follow-up turn — the async question is left untouched in its slot (still pending) and is
        # only ever auto-resolved if the session later goes terminal with it still outstanding
        # (Task 6 terminal survival -> reason="executor_finished"), never by this fall-through.
        if sess.snapshot().get("control_state") == "idle":
            return self.send_turn(session_id, answer)
        return sess.respond(answer, decision_id=decision_id)

    def send_turn(self, session_id, text):
        # Idle follow-up entry: RE-ACQUIRE an active slot before resuming. An idle session freed its
        # active slot, so resuming it must not push active+reserved past concurrency_limit; a capacity
        # refusal types nothing (mirrors start's honest cap). The reservation is held across the
        # (lockless) PTY write so a concurrent start cannot claim the same slot mid-resume.
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return RespondOutcome("unknown_session")
            if self._active_count() + self._reserved >= self._limit:
                if self._logger is not None:
                    self._logger.warning("manager", "send_turn_rejected",
                                         reason="concurrency_limit", session_id=session_id)
                return RespondOutcome("at_capacity")
            self._reserved += 1
        try:
            return sess.send_turn(text)
        finally:
            with self._lock:
                self._reserved -= 1

    def status(self, session_id=None, include_progress=False):
        """`include_progress` (Task 8): the explicit on-demand detail surface — merge
        `Session.progress_view()` into the returned snapshot(s) even during active-working, where
        `snapshot()` itself deliberately omits progress (anti-poll gate, session.py ~1260). Default
        False keeps today's behavior byte-for-byte: nothing merged, no snapshot() gate bypassed."""
        if session_id is not None:
            with self._lock:
                sess = self._sessions.get(session_id)
            if sess is None:
                return {"error": "unknown session"}
            cursor = self._events.latest_seq(session_id)   # BEFORE snapshot: never arm past unseen
            snap = sess.snapshot()
            if include_progress:
                snap.update(sess.progress_view())
            snap["cursor"] = cursor
            return snap
        with self._lock:
            cursor = self._events.latest_seq()             # GLOBAL cursor (unchanged contract)
            per_seq = self._events.latest_seqs(self._sessions.keys())  # one _cv pass under manager._lock
            snapshot = dict(self._sessions)
            now = self._clock()
            self._terminal = {sid: (snap, exp) for sid, (snap, exp) in self._terminal.items()
                              if exp > now}
            # Same expiry sweep, same policy, as self._terminal (Task 6): opportunistic purge here
            # keeps both stores from growing unbounded; respond()'s own lookup also re-checks
            # exp > now at read time, so a missed sweep is never a correctness issue, only bookkeeping.
            self._terminal_async = {sid: (rec, exp) for sid, (rec, exp) in self._terminal_async.items()
                                    if exp > now}
            recent = {sid: snap for sid, (snap, exp) in self._terminal.items()}
        sessions = {}
        for sid, s in snapshot.items():
            s_snap = s.snapshot()
            if include_progress:
                s_snap.update(s.progress_view())
            sessions[sid] = {**s_snap, "seq": per_seq.get(sid, 0)}
        return {"sessions": sessions,
                "limit": self._limit,
                "cursor": cursor,
                "recent_terminal": recent}

    def models(self, executor):
        """nelix-g9k: read-only model discovery. LOCKLESS — reads the immutable `_specs` and runs
        the executor's configured `models_cmd` with its RESOLVED env, relaying stdout. Never holds
        `self._lock` across the subprocess and touches no session / capacity state. Returns
        `(text, truncated)`.

        Raises (each mapped to a distinct HTTP code by the /models route, never a generic 500):
          - ValueError           unknown executor              -> 404
          - ModelsNotConfigured  no `models_cmd` configured     -> 400 (relayable; don't retry)
          - EnvResolveError      an `env_cmd` failed to resolve -> 502 (redacted)
          - ModelsCmdError       `models_cmd` failed/oversized  -> 502 (redacted: only the reason)
        """
        spec = self._specs.get(executor)
        if spec is None:
            raise ValueError(f"unknown executor: {executor!r} (configured: {sorted(self._specs)})")
        if spec.models_cmd is None:
            raise ModelsNotConfigured(executor)
        # The SAME env the child would get at spawn (minus hook injection): resolved_env() (os.environ
        # + expanded static [env]) with env_cmd merged OVER it, so models_cmd can reference a
        # c5o-resolved secret. Re-runs env_cmd per call (no caching) — a fresh secret-backend fetch,
        # always current. An env_cmd failure raises EnvResolveError here (before models_cmd runs).
        env = {**spec.resolved_env(),
               **resolve_env_cmds(spec.env_cmd, os.environ, spec.env_cmd_timeout_seconds)}
        value, reason = _run_capture(spec.models_cmd, env, spec.models_cmd_timeout_seconds,
                                     _MODELS_MAX_BYTES)
        if reason is not None:
            # Redacted: only the reason crosses the boundary (never the command / stdout / stderr).
            raise ModelsCmdError(reason)
        # `truncated` is False under this fail-closed policy: an over-cap model list surfaces as a
        # redacted output_too_large (502) above, never a silent truncation. The flag is kept in the
        # return contract (and on the wire) so a future soft-truncate policy stays shape-compatible.
        return value, False

    def stop(self, session_id, reason="user_stop"):
        with self._lock:
            sess = self._sessions.get(session_id)   # look up only; DO NOT pop here
        if sess is None:
            return StopOutcome("unknown_session")
        # Release the lock before sess.stop(): it joins the monitor, whose finalization re-enters
        # manager._lock via _free_slot to capture the terminal snapshot into self._terminal.
        sess.stop()
        with self._lock:
            entry = self._terminal.get(session_id)
        snap = entry[0] if entry is not None else None
        if snap is not None and snap.get("terminal_kind") == "stopped":
            status = "stopped"                       # Invariant B: confirmed terminal
        else:
            status = "stop_requested"                # teardown not confirmed within the bounded join
            snap = {**(snap or {}), "session_id": session_id,
                    "control_state": "stopping", "pending": False}
        if self._logger is not None:
            self._logger.info("manager", "session_stopped", session_id=session_id,
                              reason=reason, status=status)
        return StopOutcome(status, snapshot=snap)

    def stop_all(self, reason="shutdown"):
        with self._lock:
            sids = list(self._sessions)
        for sid in sids:
            self.stop(sid, reason=reason)
