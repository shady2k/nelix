"""Message-plane types & validation (executor -> orchestrator async messages).

An executor (a Claude Code session, etc.) posts an async message to the orchestrator without
blocking on a reply: either a `question` it wants answered later or a `note` reporting progress.
`parse_message_body` is the sole entry point that turns an untyped JSON body into one of the two
typed value objects below, or a structured `(http_status, error_msg)` error. Pure: stdlib only, no
I/O, no clock — the HTTP route (a later task) owns the transport and the session-state wiring.
"""
from dataclasses import dataclass
from typing import Optional

from daemon.config import MAX_BODY_LEN, MAX_SUMMARY_LEN


@dataclass(frozen=True)
class AsyncQuestion:
    """An executor's question that doesn't block its own progress: `continuation_plan` is what
    the executor will do while it waits for an answer."""
    question: str
    continuation_plan: str
    assumption: Optional[str] = None
    impact_if_wrong: Optional[str] = None


@dataclass(frozen=True)
class ProgressNote:
    """A one-line progress update, with optional longer-form detail."""
    summary: str
    details: Optional[str] = None


def _required_str(body, key):
    """A present, non-empty string field, else None (missing/absent/wrong-type all -> None;
    the caller turns that into the field's "required" error)."""
    v = body.get(key)
    return v if isinstance(v, str) and v else None


def _optional_str(body, key):
    """A present string field, else None (missing/wrong-type -> None; never raises on a
    malformed optional field)."""
    v = body.get(key)
    return v if isinstance(v, str) else None


def _truncate(s, cap):
    return s if s is None else s[:cap]


def parse_message_body(kind, body):
    """Validate and coerce a raw `{kind, body}` async-message POST into a typed value object.
    Returns `(obj, None)` on success or `(None, (http_status, error_msg))` on failure. Required
    fields are checked in a fixed order (question before continuation_plan; summary for a note);
    over-cap strings are TRUNCATED, never rejected."""
    if kind == "question":
        question = _required_str(body, "question")
        if question is None:
            return None, (400, "question required")
        continuation_plan = _required_str(body, "continuation_plan")
        if continuation_plan is None:
            return None, (400, "continuation_plan required")
        return AsyncQuestion(
            question=_truncate(question, MAX_BODY_LEN),
            continuation_plan=_truncate(continuation_plan, MAX_BODY_LEN),
            assumption=_truncate(_optional_str(body, "assumption"), MAX_BODY_LEN),
            impact_if_wrong=_truncate(_optional_str(body, "impact_if_wrong"), MAX_BODY_LEN),
        ), None

    if kind == "note":
        summary = _required_str(body, "summary")
        if summary is None:
            return None, (400, "summary required")
        return ProgressNote(
            summary=_truncate(summary, MAX_SUMMARY_LEN),
            details=_truncate(_optional_str(body, "details"), MAX_BODY_LEN),
        ), None

    return None, (400, "bad kind")
