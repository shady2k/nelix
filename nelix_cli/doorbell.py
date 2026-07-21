"""The wake block a host injects into the model's context.

Two hard rules, both learned the expensive way:
  * TRIAGE FIELDS ONLY. The wake rides a bounded, tail-truncating channel; an executor's screen
    excerpt is large enough to push the actionable fields out of it. Authoritative state is pulled
    afterwards with `nelix rpc status`, an uncapped channel.
  * THE BLOCK TEACHES ITS OWN CONTINUATION. It ends with the exact re-arm command carrying the
    advanced cursor, so a woken model never has to remember syntax — the loop's only fragile
    joint is re-arming, and this is what removes the need to remember it.
"""
DOORBELL_FIELDS = ("session_id", "seq", "kind", "requires_response", "hung")


def classify(body: dict) -> dict:
    """Turn a router /wait reply into {"reason", "events", "cursor"}. `reason` is one of
    "event" (something happened), "resync" (the cursor is unusable — read the board), "empty"
    (this orchestration has nothing to wait on), "none" (the window closed quietly).

    Only "none" is CONTINUABLE — every other reason is terminal for the waiter's window loop.
    "empty" matters most: the router answers it instantly, so treating it as continuable would
    spin the loop at full speed instead of waiting."""
    cursor = body.get("cursor")
    if body.get("cursor_expired") or body.get("board_changed"):
        return {"reason": "resync", "events": [], "cursor": cursor}
    if body.get("empty_orchestration"):
        return {"reason": "empty", "events": [], "cursor": cursor}
    event = body.get("event")
    if not isinstance(event, dict):
        return {"reason": "none", "events": [], "cursor": cursor}
    return {"reason": "event",
            "events": [{k: event.get(k) for k in DOORBELL_FIELDS}],
            "cursor": cursor}


def render(classified: dict, *, owner: str, orchestration: str) -> str:
    """The human+machine block. Last line is ALWAYS the re-arm command."""
    lines = [f"NELIX WAKE  orchestration={orchestration}"]
    if classified["reason"] == "resync":
        lines.append("cursor unusable (expired or the board changed) — read the board, then re-arm")
    elif classified["reason"] == "empty":
        lines.append("this orchestration has no sessions to wait on — nothing is running")
    elif classified["reason"] == "none":
        lines.append("no events in this window")
    else:
        for e in classified["events"]:
            needs = "needs an answer" if e.get("requires_response") else "no answer needed"
            hung = ", hung" if e.get("hung") else ""
            lines.append(f"  {e.get('session_id')}  {e.get('kind')}  ({needs}{hung}, seq {e.get('seq')})")
    lines.append(f"Read the board: nelix rpc status --owner {owner}")
    rearm = f"Re-arm: nelix wait --owner {owner} --orchestration {orchestration}"
    if classified.get("cursor"):
        rearm += f" --cursor {classified['cursor']}"
    lines.append(rearm)
    return "\n".join(lines)
