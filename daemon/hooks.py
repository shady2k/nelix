"""Claude hook payload → typed observation (spec §hook normalization). Pure data + one mapper —
stdlib only, no clock, no I/O.

A Claude-Code agent reports its own lifecycle through hooks (ground truth). `HookEvent` is the
parsed POST body; `HookObservation` is the normalized, driver-agnostic reading the belief engine
consumes. `normalize_claude_hook` is the sole hook-event → state mapper: it names WHAT the event
means (working / waiting_for_user / idle and the turn-boundary flags); all temporal interpretation
and precedence are the core's (daemon.belief.BeliefEngine), which is why this module carries no
timestamps and no state.
"""
import hashlib
import json
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
    gate_fp: Optional[str] = None   # per-gate fingerprint of a waiting_for_user pause (nelix-f7y)


def gate_fingerprint(tool_name: Optional[str], tool_input: Optional[dict]) -> Optional[str]:
    """A stable per-gate identity derived from the tool call ITSELF: a hash of tool_name + the
    canonical (key-sorted) tool_input. Both hooks of ONE physical gate carry IDENTICAL
    tool_name/tool_input — the collision's PreToolUse[AskUserQuestion] and its PermissionRequest both
    carry the SAME {questions:[...]}, a Bash gate's PreToolUse and PermissionRequest both the SAME
    {command,...} — so they share a fingerprint (dedup); different gates carry different tool_input (so
    a genuinely new gate is distinct). This is the ONLY per-gate identity available: Claude's
    PermissionRequest hook carries NO tool_use_id (only PreToolUse does), so cross-hook id correlation
    is impossible; the body fingerprint needs neither a shared id nor any hook-ordering assumption
    (nelix-f7y). Returns None when tool_input is absent/empty (defensive; real gates always carry it)."""
    if not tool_input:
        return None
    canonical = json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(f"{tool_name or ''}\x00{canonical}".encode()).hexdigest()[:16]


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

    # A tool wants permission -> the agent is blocked on the user. Carry the per-gate fingerprint: the
    # belief engine keys the pause by it, so a straggler of THIS gate dedups (identical tool_input ->
    # same fingerprint) while a genuinely new gate is distinct (different tool_input) — no epoch-level
    # ambiguity, no hook-ordering assumption, and no need for a shared id the PermissionRequest lacks.
    if event == "PermissionRequest":
        return HookObservation(kind="waiting_for_user", closes_turn=False, opens_turn=False,
                               clears_pending=False, prompt_kind="permission_choice",
                               raw_event=event, gate_fp=gate_fingerprint(ev.tool_name, ev.tool_input))

    # AskUserQuestion: PreToolUse raises the modal (waiting); PostToolUse resolves it (back to work).
    if ev.tool_name == "AskUserQuestion":
        if event == "PreToolUse":
            return HookObservation(kind="waiting_for_user", closes_turn=False, opens_turn=False,
                                   clears_pending=False, prompt_kind="modal_choice",
                                   raw_event=event, gate_fp=gate_fingerprint(ev.tool_name, ev.tool_input))
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
