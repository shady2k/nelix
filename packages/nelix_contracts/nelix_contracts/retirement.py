"""When a generation may exit. Pure predicate; Plan 4 builds the machinery around it.

"N-1 exits at zero" is WRONG as a rule: zero live PTYs is not zero routable state (design
§5). After a worker exits, callers still need to discover the terminal result, acknowledge
it, read the transcript, or receive the FINAL event if they were not watching. If the
generation exits when its live-session table empties, its in-memory event ring and terminal
inventory vanish with it.

Visibility is expressed as MONOTONIC WATERMARKS rather than a boolean, because a boolean
goes stale: the router confirms through record 7, the flag flips true, record 8 is
persisted, and a naive check passes before 8 is visible. A watermark comparison cannot be
fooled that way.

The ordering this encodes:
    publish terminal event -> persist generation-neutral record -> router makes it visible
      -> remove live session -> router confirms -> generation may exit

S5a: generation-level oracle — a generation retires iff it has no live/current incarnation
AND every epoch has retirement_state=certified AND confirmed_high_water >= final_high_water.
The oracle checks retirement_state, NEVER process_state.
"""


def _count(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative int: {value!r}")
    return value


def retirement_blockers(*, live_pty_count: int, inflight_or_starting_count: int,
                        terminal_persisted_high_water: int,
                        router_visible_high_water: int) -> tuple:
    """Every reason this generation may not exit yet, in a stable order.

    Returns all of them rather than the first, so an operator sees the whole picture instead
    of playing whack-a-mole.
    """
    _count(live_pty_count, "live_pty_count")
    _count(inflight_or_starting_count, "inflight_or_starting_count")
    _count(terminal_persisted_high_water, "terminal_persisted_high_water")
    _count(router_visible_high_water, "router_visible_high_water")

    blockers = []
    if live_pty_count > 0:
        blockers.append("live_ptys")
    # A reservation already assigned to this generation but with no PTY yet: retiring now
    # would land the start on a dead backend.
    if inflight_or_starting_count > 0:
        blockers.append("starts_in_flight")
    if router_visible_high_water < terminal_persisted_high_water:
        blockers.append("terminal_records_not_yet_visible_to_router")
    return tuple(blockers)


def may_retire(*, live_pty_count: int, inflight_or_starting_count: int,
               terminal_persisted_high_water: int, router_visible_high_water: int) -> bool:
    return not retirement_blockers(
        live_pty_count=live_pty_count,
        inflight_or_starting_count=inflight_or_starting_count,
        terminal_persisted_high_water=terminal_persisted_high_water,
        router_visible_high_water=router_visible_high_water)


# ---- S5a: generation-level oracle (aggregate check across ALL epochs) ----


def generation_retirement_oracle_blockers(*, store, generation_id: str) -> tuple:
    """Check whether a generation may be retired, reading from the store.

    Returns blockers (empty tuple = clear to retire). A generation retires iff:
    - It has no live/current incarnation (current_epoch is NULL)
    - Every epoch has retirement_state=certified
    - For every epoch, confirmed_high_water >= final_high_water
    - Checks retirement_state, NEVER process_state
    """
    blockers = []

    gen = store.get_generation(generation_id)
    if gen.current_epoch is not None:
        blockers.append("has_current_epoch")

    epochs = store.list_epochs(generation_id)
    if not epochs:
        blockers.append("no_epochs")
        return tuple(blockers)

    for ep in epochs:
        if ep.retirement_state != "certified":
            blockers.append(f"epoch_not_certified:{ep.generation_epoch}")
        else:
            confirmed = store.get_generation_confirmed_high_water(ep.generation_epoch)
            if confirmed < (ep.final_high_water or 0):
                blockers.append(
                    f"confirmed_below_final:{ep.generation_epoch}"
                    f"({confirmed}<{ep.final_high_water})")

    return tuple(blockers)


def generation_may_retire(*, store, generation_id: str) -> bool:
    """True iff the generation-level oracle sees no blockers."""
    return not generation_retirement_oracle_blockers(
        store=store, generation_id=generation_id)
