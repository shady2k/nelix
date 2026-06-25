import itertools
import threading
import time
import uuid
from dataclasses import dataclass

RESPONDABLE_KINDS = {"waiting_for_user", "blocked"}


@dataclass
class Event:
    seq: int
    event_id: str
    session_id: str
    executor: str
    kind: str
    summary: str
    state: str
    answered: bool = False
    turn_index: int = 0
    range: tuple = (0, 0)
    hint: str = None
    hung: bool = False
    task_delivery: str = "delivered"
    requires_response: bool = False
    screen_excerpt: str = ""


class EventQueue:
    """Shared, global-ordered event log across all sessions. Owns the blocking
    long-poll wait (single condition) so one waiter can multiplex every session."""

    def __init__(self):
        self._events = []
        self._seq = itertools.count(1)
        self._cv = threading.Condition()

    def publish(self, session_id, executor, kind, summary, state, *,
                turn_index=0, range=(0, 0), hint=None, hung=False,
                task_delivery="delivered", requires_response=False, screen_excerpt=""):
        with self._cv:
            e = Event(next(self._seq), f"evt-{uuid.uuid4().hex[:8]}", session_id, executor,
                      kind, summary, state, turn_index=turn_index, range=range,
                      hint=hint, hung=hung, task_delivery=task_delivery,
                      requires_response=requires_response, screen_excerpt=screen_excerpt)
            self._events.append(e)
            self._cv.notify_all()
            return e

    def latest_after(self, after_seq):
        for e in self._events:
            if e.seq > after_seq:
                return e
        return None

    def wait_event(self, after_seq, timeout):
        deadline = time.time() + timeout
        with self._cv:
            while True:
                evt = self.latest_after(after_seq)
                if evt is not None:
                    return evt
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)

    def mark_answered(self, event_id):
        with self._cv:
            for e in self._events:
                if e.event_id == event_id:
                    e.answered = True
                    return True
            return False

    def pending(self, session_id=None):
        with self._cv:
            for e in reversed(self._events):
                if e.kind in RESPONDABLE_KINDS and not e.answered:
                    if session_id is None or e.session_id == session_id:
                        return e
            return None
