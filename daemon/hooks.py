"""Claude hook payload → typed observation (spec §hook normalization). Pure data + one mapper —
stdlib only, no clock, no I/O.

A Claude-Code agent reports its own lifecycle through hooks (ground truth). `HookEvent` is the
parsed POST body; `HookObservation` is the normalized, driver-agnostic reading the belief engine
consumes. `normalize_claude_hook` is the sole hook-event → state mapper: it names WHAT the event
means (working / waiting_for_user / idle and the turn-boundary flags); all temporal interpretation
and precedence are the core's (daemon.belief.BeliefEngine), which is why this module carries no
timestamps and no state.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class HookEvent:
    """A parsed Claude hook POST body. `event` is the raw `hook_event_name`."""
    session_id: str
    event: str
    tool_name: Optional[str] = None
    tool_input: dict = field(default_factory=dict)
    is_interrupt: bool = False
    notification: Optional[str] = None   # matcher/message for Notification events
    tool_use_id: Optional[str] = None    # per-tool-call id: SAME across one invocation's hooks (nelix-f7y)


@dataclass(frozen=True)
class HookObservation:
    """The normalized reading of one hook event. `kind` is the state it implies; the boolean flags
    mark turn boundaries the engine uses to order events within a turn epoch."""
    kind: str                  # "working" | "waiting_for_user" | "idle"
    closes_turn: bool          # True for Stop/StopFailure (and idle Notification)
    opens_turn: bool           # True for UserPromptSubmit
    clears_pending: bool       # True for PostToolUse[AskUserQuestion]
    interrupted: bool = False
    prompt_kind: Optional[str] = None   # "permission_choice"|"modal_choice"|"free_text"|None
    raw_event: str = ""
    tool_use_id: Optional[str] = None   # stable per-gate id for a waiting_for_user pause (nelix-f7y)


def normalize_claude_hook(ev: HookEvent) -> HookObservation:
    """Map a single Claude hook event to its typed observation. Pure: no state, no clock."""
    event = ev.event

    # Turn close: the agent finished (or aborted) its turn -> idle.
    if event in ("Stop", "StopFailure"):
        return HookObservation(kind="idle", closes_turn=True, opens_turn=False,
                               clears_pending=False, interrupted=ev.is_interrupt,
                               raw_event=event)

    # Turn open: a fresh user prompt starts a working turn.
    if event == "UserPromptSubmit":
        return HookObservation(kind="working", closes_turn=False, opens_turn=True,
                               clears_pending=False, raw_event=event)

    # A tool wants permission -> the agent is blocked on the user. Carry the tool_use_id: the belief
    # engine keys the pause by it (per-gate), so a straggler of THIS gate dedups (same id) while a
    # genuinely new gate is distinct (different id) — no epoch-level ambiguity (nelix-f7y).
    if event == "PermissionRequest":
        return HookObservation(kind="waiting_for_user", closes_turn=False, opens_turn=False,
                               clears_pending=False, prompt_kind="permission_choice",
                               raw_event=event, tool_use_id=ev.tool_use_id)

    # AskUserQuestion: PreToolUse raises the modal (waiting); PostToolUse resolves it (back to work).
    if ev.tool_name == "AskUserQuestion":
        if event == "PreToolUse":
            return HookObservation(kind="waiting_for_user", closes_turn=False, opens_turn=False,
                                   clears_pending=False, prompt_kind="modal_choice",
                                   raw_event=event, tool_use_id=ev.tool_use_id)
        if event == "PostToolUse":
            return HookObservation(kind="working", closes_turn=False, opens_turn=False,
                                   clears_pending=True, raw_event=event)

    # Notification carries its own meaning in the message/matcher text.
    if event == "Notification":
        note = ev.notification or ""
        if note.startswith("permission"):
            return HookObservation(kind="waiting_for_user", closes_turn=False, opens_turn=False,
                                   clears_pending=False, prompt_kind="permission_choice",
                                   raw_event=event)
        if note.startswith("idle"):
            return HookObservation(kind="idle", closes_turn=True, opens_turn=False,
                                   clears_pending=False, raw_event=event)
        return HookObservation(kind="working", closes_turn=False, opens_turn=False,
                               clears_pending=False, raw_event=event)

    # Every other tool-lifecycle event (Pre/PostToolUse, PostToolUseFailure, ...) is the agent
    # actively working.
    return HookObservation(kind="working", closes_turn=False, opens_turn=False,
                           clears_pending=False, raw_event=event)
