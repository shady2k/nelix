import itertools
import threading

from daemon.session import Session


def _default_session_factory(sid, executor, spec, events, launcher_factory,
                             driver_factory, logger):
    return Session(sid, executor, driver_factory(spec.driver),
                   launcher_factory(spec.launcher), spec, events, logger=logger)


class SessionManager:
    """Registry of sessions. MVP holds <= concurrency_limit (default 1)."""

    def __init__(self, specs, events, launcher_factory=None, driver_factory=None,
                 concurrency_limit=1, logger=None, session_factory=None):
        self._specs = specs
        self._events = events
        self._limit = concurrency_limit
        self._logger = logger
        self._sessions = {}
        self._ids = (f"s{n}" for n in itertools.count(1))
        self._lock = threading.Lock()
        if session_factory is not None:
            self._make = lambda sid, ex, spec: session_factory(sid, ex, spec, events)
        else:
            self._make = lambda sid, ex, spec: _default_session_factory(
                sid, ex, spec, events, launcher_factory, driver_factory, logger)

    def start(self, executor_name, task):
        spec = self._specs.get(executor_name)
        if spec is None:
            raise RuntimeError(f"unknown executor: {executor_name!r} "
                               f"(configured: {sorted(self._specs)})")
        with self._lock:
            if len(self._sessions) >= self._limit:
                raise RuntimeError(
                    f"concurrency_limit={self._limit} reached "
                    f"(active: {sorted(self._sessions)}); concurrent executors are post-MVP")
            sid = next(self._ids)
            sess = self._make(sid, executor_name, spec)
            self._sessions[sid] = sess
        sess.start(task)
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
