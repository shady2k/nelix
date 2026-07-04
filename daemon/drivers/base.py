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
    # CLI flag that selects the per-session model (nelix-9k0), or None if this CLI cannot express a
    # model override. Structural typing: a concrete driver does NOT inherit this default — it must
    # declare it to advertise support, and every read uses getattr(driver, "model_flag", None) so a
    # driver that omits it is treated as "no override support" (never an AttributeError).
    model_flag: "str | None" = None
    # nelix-kwr: discovery protocol key (drives daemon/model_discovery). None = the driver's backend
    # exposes no model list (pre-flight validation is skipped). Structural typing like model_flag.
    models_protocol: "str | None" = None
    # nelix-kwr: model names that are always valid (tier aliases the CLI remaps via env defaults, so
    # they never appear in /v1/models). Matched case-insensitively; the original string is passed to
    # the CLI unchanged.
    model_aliases: frozenset = frozenset()
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
