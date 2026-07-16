"""When a generation may exit. Pure predicate; Plan 4 builds the machinery around it.

"N-1 exits at zero" is WRONG as a rule: zero live PTYs is not zero routable state (design
§5). After a worker exits, callers still need to discover the terminal result, acknowledge
it, read the transcript, or receive the FINAL event if they were not watching. If the
generation exits when its live-session table empties, its in-memory event ring and terminal
inventory vanish with it.

Hence the ordering invariant this encodes:
    publish terminal event -> persist generation-neutral record -> router makes it visible
      -> remove live session -> router confirms -> generation may exit
"""


def retirement_blockers(*, live_pty_count: int, unpersisted_terminal_count: int,
                        router_confirmed_visible: bool) -> tuple:
    """Every reason this generation may not exit yet, in a stable order.

    Returns all of them rather than the first, so an operator sees the whole picture instead
    of playing whack-a-mole.
    """
    blockers = []
    if live_pty_count > 0:
        blockers.append("live_ptys")
    if unpersisted_terminal_count > 0:
        blockers.append("unpersisted_terminal_records")
    if not router_confirmed_visible:
        blockers.append("router_has_not_confirmed_visibility")
    return tuple(blockers)


def may_retire(*, live_pty_count: int, unpersisted_terminal_count: int,
               router_confirmed_visible: bool) -> bool:
    return not retirement_blockers(
        live_pty_count=live_pty_count,
        unpersisted_terminal_count=unpersisted_terminal_count,
        router_confirmed_visible=router_confirmed_visible)
