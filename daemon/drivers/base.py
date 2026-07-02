from typing import Protocol

from daemon.observation import Observation, ObservationCtx


class Driver(Protocol):
    """The SOLE driver contract (spec §5.5/§5.6). `observe()` reports visual facts as one
    Observation value; the generic core (BeliefEngine) owns all temporal interpretation. The
    driver owns the KEYS (actuation returns the sequence to send); Session owns every PTY write.

    The legacy single-verdict classifier and its folded screen predicates are gone — their logic now
    lives entirely in `observe()` as Observation fields (prompt_kind / affordances / options /
    submitted_echo_present).
    """
    hook_capable: bool = False   # True if this CLI reports its own lifecycle via nelix hooks
                                 # (launcher injects --settings + NELIX_* env); else screen-only.
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
