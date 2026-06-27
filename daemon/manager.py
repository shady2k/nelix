import os
import shutil
import threading
import time
import uuid

import paths
from daemon.events import EXTERNAL_OUTPUT_POLICY
from daemon.session import RespondOutcome, Session


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
                 concurrency_limit=5, logger=None, session_factory=None,
                 session_retain=20, session_max_age_days=7, reaper_ctx=None,
                 terminal_snapshot_ttl=300.0, clock=time.time):
        self._specs = specs
        self._events = events
        self._limit = concurrency_limit
        self._logger = logger
        self._session_retain = session_retain
        self._session_max_age_days = session_max_age_days
        self._reaper_ctx = reaper_ctx
        self._sessions = {}
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
            if len(self._sessions) >= self._limit:
                if self._logger is not None:
                    self._logger.warning("manager", "session_start_rejected",
                                         reason="concurrency_limit", executor=executor_name)
                raise RuntimeError(
                    f"concurrency_limit={self._limit} reached "
                    f"(active: {sorted(self._sessions)})")
            sid = f"s-{uuid.uuid4().hex[:8]}"
            base_seq = self._events.latest_seq()      # waiter arms past anything already emitted
            sess = self._make(sid, executor_name, spec)
            sess.on_terminal = self._free_slot
            sess.reaper_ctx = self._reaper_ctx
            self._sessions[sid] = sess
            keep = set(self._sessions)
        if self._logger is not None:
            self._logger.info("manager", "session_created", session_id=sid,
                              executor=executor_name, cwd=cwd,
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
                self._sessions.pop(sid, None)     # (e.g. a rejected task or a spawn failure)
            if self._logger is not None:
                self._logger.error("manager", "session_start_failed", session_id=sid, exc_info=True)
            raise
        return sid, base_seq

    def _free_slot(self, session_id):
        with self._lock:
            sess = self._sessions.get(session_id)
            snap = None
            if sess is not None:
                try:
                    snap = sess.terminal_snapshot()
                except Exception:
                    snap = None
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
            return {"state": "working", "pending": False,
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
        return sess.respond(answer, decision_id=decision_id)

    def status(self, session_id=None):
        if session_id is not None:
            with self._lock:
                sess = self._sessions.get(session_id)
            return sess.snapshot() if sess else {"error": "unknown session"}
        with self._lock:
            cursor = self._events.latest_seq()        # BEFORE snapshots: never advance past an unseen event
            snapshot = dict(self._sessions)
            now = self._clock()
            self._terminal = {sid: (snap, exp) for sid, (snap, exp) in self._terminal.items()
                              if exp > now}
            recent = {sid: snap for sid, (snap, exp) in self._terminal.items()}
        return {"sessions": {sid: s.snapshot() for sid, s in snapshot.items()},
                "limit": self._limit,
                "cursor": cursor,
                "recent_terminal": recent}

    def stop(self, session_id, reason="user_stop"):
        with self._lock:
            sess = self._sessions.pop(session_id, None)
        if sess is None:
            return False
        sess.stop()
        if self._logger is not None:
            self._logger.info("manager", "session_stopped", session_id=session_id,
                              reason=reason, slot_freed=True)
        return True

    def stop_all(self, reason="shutdown"):
        with self._lock:
            sids = list(self._sessions)
        for sid in sids:
            self.stop(sid, reason=reason)
