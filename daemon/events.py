import itertools
import uuid
from dataclasses import dataclass


@dataclass
class Event:
    seq: int
    event_id: str
    kind: str
    summary: str
    state: str
    answered: bool = False


class EventQueue:
    def __init__(self):
        self._events = []
        self._seq = itertools.count(1)

    def publish(self, kind, summary, state):
        e = Event(next(self._seq), f"evt-{uuid.uuid4().hex[:8]}", kind, summary, state)
        self._events.append(e)
        return e

    def latest_after(self, after_seq):
        for e in self._events:
            if e.seq > after_seq:
                return e
        return None

    def mark_answered(self, event_id):
        for e in self._events:
            if e.event_id == event_id:
                e.answered = True
                return True
        return False

    def pending(self):
        for e in reversed(self._events):
            if e.kind == "waiting_for_user" and not e.answered:
                return e
        return None
