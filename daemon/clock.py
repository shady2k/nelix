"""Injectable clock seam (spec §5.7, BLOCKER-5).

The belief path takes an injected clock instead of calling time.monotonic()/time.sleep()
directly, so a recorded capture can drive BeliefEngine.tick deterministically by advancing a
FakeClock by the recorded inter-byte deltas — no real sleeps, no wall-clock nondeterminism.
"""
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class WallClock:
    """Production clock: monotonic elapsed time (the belief path only ever uses deltas)."""

    def now(self) -> float:
        return time.monotonic()


class FakeClock:
    """Test/replay clock: starts at `start`, advanced explicitly by `advance(dt)`."""

    def __init__(self, start: float = 0.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, dt: float) -> float:
        self._t += dt
        return self._t
