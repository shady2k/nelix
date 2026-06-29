"""Driver observation contract (spec §5.5). Pure data — stdlib only, no clock, no logic.

`observe(frame, ctx) -> Observation` is the SOLE driver classification contract. The driver
reports visual facts (what the screen IS, what it affords, whether our submission is still
echoed, fingerprints, an on-screen-only busy hint); all temporal interpretation is the core's
(daemon.belief.BeliefEngine), which is why this module carries no timestamps and no clock.
"""
from dataclasses import dataclass, field
from typing import Literal, Optional

# What the screen IS (raw frame read). `none` = no prompt visible (busy).
PromptKind = Literal["none", "free_text", "modal_choice", "permission_choice",
                     "crash", "exit", "unknown"]

# Actionable handles the screen exposes (a frozenset of these string flags on Observation).
Affordance = Literal["accepts_text_input", "modal_choice", "permission_choice",
                     "interrupt_available", "background_available"]

# On-screen busy hint (chrome only, NEVER the agent's NL output). `None` = no known reason.
BusyReason = Literal["generating", "running_command", "waiting_subagents"]


@dataclass(frozen=True)
class Heartbeat:
    """The driver's liveness region. `fp` hashes that region (None when present=False).
    `present=False` => no identifiable region (=> liveness `unknown`). `expected_to_change`
    distinguishes a *static* heartbeat (unknown) from a *frozen-but-should-tick* one (stale)."""
    fp: Optional[str] = None
    present: bool = False
    expected_to_change: bool = False


@dataclass(frozen=True)
class Option:
    """A modal/permission menu choice. `id` = the selector the driver actuates (e.g. "1");
    `label` = the human text recorded in the transcript."""
    id: str
    label: str


@dataclass(frozen=True)
class Observation:
    prompt_kind: PromptKind
    affordances: frozenset = field(default_factory=frozenset)
    options: tuple = ()
    submitted_echo_present: bool = False
    semantic_fp: str = ""        # whole meaning-normalized frame (chrome zeroed)
    content_fp: str = ""         # meaning-normalized frame EXCLUDING the active input region
    prompt_fp: str = ""          # the prompt/affordance region only
    heartbeat: Heartbeat = field(default_factory=Heartbeat)
    busy_reason: Optional[str] = None
    ask_mode: bool = False       # folded is_ask_mode: the agent will surface permission prompts


@dataclass(frozen=True)
class ObservationCtx:
    """The non-screen facts observe() needs: our last submission (for echo detection) and the
    child's liveness/exit (from which prompt_kind crash/exit is derived)."""
    last_submitted_text: Optional[str]
    child_alive: bool
    exit_code: Optional[int]
