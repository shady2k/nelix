from dataclasses import dataclass
from typing import Literal, Optional, Protocol

DriverState = Literal["working", "quiet_working", "idle_prompt",
                      "permission_prompt", "crashed", "exited"]


@dataclass
class ClassifyCtx:
    stable_for: float
    bytes_idle_for: float
    child_alive: bool
    exit_code: Optional[int] = None


class Driver(Protocol):
    ask_mode_toggle: str

    def normalize_frame(self, frame: str) -> str: ...
    def classify(self, frame: str, ctx: ClassifyCtx) -> DriverState: ...
    def is_ask_mode(self, frame: str) -> bool: ...
