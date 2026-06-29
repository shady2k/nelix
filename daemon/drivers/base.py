from dataclasses import dataclass
from typing import Optional, Protocol

from daemon.observation import Observation, ObservationCtx


# ClassifyCtx is the legacy per-tick context consumed by the delivery loop's settle gate. It is
# removed together with the classify-based delivery path when Session is migrated to observe()
# (Task 8); the rich observe() contract replaces it. Kept here only while Session still uses it.
@dataclass
class ClassifyCtx:
    stable_for: float
    bytes_idle_for: float
    child_alive: bool
    exit_code: Optional[int] = None


class Driver(Protocol):
    """The SOLE driver contract (spec §5.5/§5.6). `observe()` reports visual facts as one
    Observation value; the generic core (BeliefEngine) owns all temporal interpretation. The
    driver owns the KEYS (actuation returns the sequence to send); Session owns every PTY write.

    `classify`/the six-state vocabulary and the folded predicates
    (`is_accepting_input`/`is_modal_choice`/`is_ask_mode`/`input_submission_present`) are removed —
    their logic lives in `observe()` as Observation fields.
    """
    ask_mode_toggle: str
    command_prefixes: tuple      # leading tokens the CLI reads as a command, not a prompt
    submit_key: str              # the key that submits a line (CR for most TUIs)

    def normalize_frame(self, frame: str) -> str: ...
    def observe(self, frame: str, ctx: ObservationCtx) -> Observation: ...
    def is_transcript_volatile(self, row: str) -> bool: ...   # row is terminal chrome, not content

    # Actuation — each returns the key SEQUENCE to send (Session encodes + writes it to the PTY;
    # drivers never touch the PTY). The driver owns the tool-specific keys; the core owns the write.
    def format_submission(self, text: str) -> str: ...   # framing for a typed free-text submission
    def submit_text(self, text: str) -> str: ...         # a free-text answer (no submit key)
    def select_option(self, id: str) -> str: ...         # pick a modal option (digit + confirm)
    def interrupt(self) -> str: ...                      # the interrupt key (ESC for claude)
