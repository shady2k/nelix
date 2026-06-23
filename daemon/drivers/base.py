from typing import Literal, Protocol

DriverState = Literal["working", "waiting_for_user", "done_candidate",
                      "crashed", "idle"]


class Driver(Protocol):
    ask_mode_toggle: str

    def is_task_accepted_signal(self, grid: str) -> bool: ...
    def classify(self, grid: str, task_accepted: bool) -> DriverState: ...
    def is_ask_mode(self, grid: str) -> bool: ...
