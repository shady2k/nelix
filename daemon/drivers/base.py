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
    command_prefixes: tuple      # leading tokens the CLI reads as a command, not a prompt
    submit_key: str              # the key that submits a line (CR for most TUIs)

    def normalize_frame(self, frame: str) -> str: ...
    def classify(self, frame: str, ctx: ClassifyCtx) -> DriverState: ...
    def is_ask_mode(self, frame: str) -> bool: ...
    def is_accepting_input(self, frame: str) -> bool: ...
    def is_modal_choice(self, frame: str) -> bool: ...
    def input_echo_present(self, frame: str, text: str) -> bool: ...
