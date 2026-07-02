import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass

import paths
from daemon.events import EXTERNAL_OUTPUT_POLICY
from daemon.session import RespondOutcome, Session


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
        self._terminal_ttl = terminal_snapshot_ttl
        self._clock = clock
        self._lock = threading.Lock()
        if session_factory is not None:
            self._make = lambda sid, ex, spec: session_factory(sid, ex, spec, events)
        else:
            self._make = lambda sid, ex, spec: _default_session_factory(
                sid, ex, spec, events, launcher_factory, driver_factory, logger)

    def start(self, executor_name, task, cwd):
        return self._spawn(executor_name, task, cwd, lineage_id=None, restarted_from=None)

    def _spawn(self, executor_name, task, cwd, *, lineage_id, restarted_from, reserve=False):
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
        except Exception:
            try:
                sess.stop()                       # tear down any partially-spawned PTY / open dialog
            except Exception:
                pass
            with self._lock:                      # don't leak a registered-but-unstarted session
                self._sessions.pop(sid, None)     # reservation already consumed: slot frees cleanly
            if self._logger is not None:
                self._logger.error("manager", "session_start_failed", session_id=sid, exc_info=True)
            raise
        return StartOutcome(session_id=sid, base_seq=base_seq, snapshot=sess.snapshot())

    def _restart_source(self, session_id):
        """Resolve (executor, task, cwd, lineage_id, active_session_or_None) for a restart.
        Source is an ACTIVE session if present, else the PERSISTED session-dir meta (the main path:
        a crashed/done session has already been removed from _sessions). Returns None if neither."""
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is not None:
            return (sess.executor, sess.task, sess.cwd,
                    sess.lineage_id or session_id, sess)
        try:
            meta = json.loads(paths.session_meta(paths.sessions_root() / session_id).read_text())
        except (OSError, ValueError):
            return None
        if not meta.get("executor") or meta.get("cwd") is None:
            return None
        return (meta["executor"], meta.get("task"), meta["cwd"],
                meta.get("lineage_id") or session_id, None)

    def restart(self, session_id, force=False):
        src = self._restart_source(session_id)
        if src is None:
            return RestartOutcome("unknown_session")
        executor, task, cwd, lineage_id, active = src
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
            started = self._spawn(executor, task, cwd, lineage_id=lineage_id,
                                  restarted_from=session_id, reserve=reserve)
            new_sid, base_seq = started.session_id, started.base_seq
        except Exception:
            if self._logger is not None:
                self._logger.error("manager", "restart_spawn_failed", session_id=session_id,
                                   exc_info=True)
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
            if sess is not None:
                try:
                    snap = sess.terminal_snapshot()
                except Exception:
                    snap = None
            # a CLEAN terminal (terminal_kind="done") ends the lineage; a crash/stop keeps it for a
            # possible restart. Keyed on terminal_kind, not a state string (NIT-16).
            if snap is not None and snap.get("terminal_kind") == "done":
                self._lineages.pop(snap.get("lineage_id"), None)
            existed = self._sessions.pop(session_id, None) is not None
            if snap is not None and self._terminal_ttl > 0:
                self._terminal[session_id] = (snap, self._clock() + self._terminal_ttl)
        if existed and self._logger is not None:
            self._logger.info("manager", "slot_freed", session_id=session_id)

    def get(self, session_id):
        with self._lock:
            return self._sessions.get(session_id)

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
                return self.send_turn(session_id, text)      # idle now -> re-acquire slot + fresh turn
            if disposition == "queued_busy":
                # busy -> the monitor delivers at the next idle (drain_async_reply). Nothing typed yet.
                return RespondOutcome("queued", snapshot=sess.snapshot())
            return RespondOutcome("not_delivered", snapshot=sess.snapshot())  # closing/terminal
        # A follow-up on an IDLE session (turn complete, alive, no respondable decision) is a NEW
        # turn: route it through send_turn (re-acquire an active slot + re-open the turn) — never
        # respond(), whose no_pending path can't drive a non-respondable idle decision (plan Task 10).
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

    def status(self, session_id=None):
        if session_id is not None:
            with self._lock:
                sess = self._sessions.get(session_id)
            if sess is None:
                return {"error": "unknown session"}
            cursor = self._events.latest_seq(session_id)   # BEFORE snapshot: never arm past unseen
            snap = sess.snapshot()
            snap["cursor"] = cursor
            return snap
        with self._lock:
            cursor = self._events.latest_seq()             # GLOBAL cursor (unchanged contract)
            per_seq = self._events.latest_seqs(self._sessions.keys())  # one _cv pass under manager._lock
            snapshot = dict(self._sessions)
            now = self._clock()
            self._terminal = {sid: (snap, exp) for sid, (snap, exp) in self._terminal.items()
                              if exp > now}
            recent = {sid: snap for sid, (snap, exp) in self._terminal.items()}
        return {"sessions": {sid: {**s.snapshot(), "seq": per_seq.get(sid, 0)}
                             for sid, s in snapshot.items()},
                "limit": self._limit,
                "cursor": cursor,
                "recent_terminal": recent}

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
