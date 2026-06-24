import os
import shutil
import threading
import time
import uuid

import paths
from daemon.session import Session


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
            logger.event("manager", "warning", msg="session gc skip", dir=str(d), err=str(e))


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
    """Registry of sessions. MVP holds <= concurrency_limit (default 1)."""

    def __init__(self, specs, events, launcher_factory=None, driver_factory=None,
                 concurrency_limit=1, logger=None, session_factory=None,
                 session_retain=20, session_max_age_days=7):
        self._specs = specs
        self._events = events
        self._limit = concurrency_limit
        self._logger = logger
        self._session_retain = session_retain
        self._session_max_age_days = session_max_age_days
        self._sessions = {}
        self._lock = threading.Lock()
        if session_factory is not None:
            self._make = lambda sid, ex, spec: session_factory(sid, ex, spec, events)
        else:
            self._make = lambda sid, ex, spec: _default_session_factory(
                sid, ex, spec, events, launcher_factory, driver_factory, logger)

    def start(self, executor_name, task, cwd):
        spec = self._specs.get(executor_name)
        if spec is None:
            raise RuntimeError(f"unknown executor: {executor_name!r} "
                               f"(configured: {sorted(self._specs)})")
        cwd = os.path.abspath(os.path.expanduser(cwd))
        with self._lock:
            if len(self._sessions) >= self._limit:
                raise RuntimeError(
                    f"concurrency_limit={self._limit} reached "
                    f"(active: {sorted(self._sessions)}); concurrent executors are post-MVP")
            sid = f"s-{uuid.uuid4().hex[:8]}"
            sess = self._make(sid, executor_name, spec)
            self._sessions[sid] = sess
            keep = set(self._sessions)
        gc_sessions(keep, self._session_retain, self._session_max_age_days, logger=self._logger)
        sess.start(task, cwd)
        return sid

    def get(self, session_id):
        return self._sessions.get(session_id)

    def respond(self, session_id, event_id, answer):
        sess = self._sessions.get(session_id)
        return bool(sess and sess.respond(event_id, answer))

    def status(self, session_id=None):
        if session_id is not None:
            sess = self._sessions.get(session_id)
            return sess.snapshot() if sess else {"error": "unknown session"}
        with self._lock:
            snapshot = dict(self._sessions)
        return {"sessions": {sid: s.snapshot() for sid, s in snapshot.items()},
                "limit": self._limit}

    def stop(self, session_id):
        with self._lock:
            sess = self._sessions.pop(session_id, None)
        if sess is None:
            return False
        sess.stop()
        return True

    def stop_all(self):
        for sid in list(self._sessions):
            self.stop(sid)
