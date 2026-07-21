"""How many long-polls are attached per orchestration, right now.

Kept as its own tiny module because two collaborators need it and neither owns it: WaitForward
increments it while it blocks, BoardForward reports it. A zero-count entry is REMOVED rather than
kept at 0, so `counts()` is exactly 'who is being listened to' with no stale keys to filter.
"""
import threading
from contextlib import contextmanager


class WaiterRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts = {}

    @contextmanager
    def attached(self, orchestration_id: str):
        """Count one attached waiter for the duration of the block — released on any exit path,
        including an exception, so a failed long-poll can never leak a phantom listener."""
        with self._lock:
            self._counts[orchestration_id] = self._counts.get(orchestration_id, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                remaining = self._counts.get(orchestration_id, 1) - 1
                if remaining > 0:
                    self._counts[orchestration_id] = remaining
                else:
                    self._counts.pop(orchestration_id, None)

    def count(self, orchestration_id: str) -> int:
        with self._lock:
            return self._counts.get(orchestration_id, 0)

    def counts(self) -> dict:
        with self._lock:
            return dict(self._counts)
