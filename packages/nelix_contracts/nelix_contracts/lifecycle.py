"""Durable lifecycle FSM for generations (§3.4).

States: ready -> active -> draining -> retiring -> retired
- ready:      minted, daemon spawned, awaiting activation.
- active:     serving sessions; this is the generation registry.active() resolves.
- draining:   no-new-sessions, still routable for existing sessions (S2b).
- retiring:   retirement oracle engaged (S5).
- retired:    fully retired (S5).

Only `ready -> active -> draining` is reachable in S4.
`retiring`/`retired` transitions require `retire` (S5, feature-disabled here).
"""

READY = "ready"
ACTIVE = "active"
DRAINING = "draining"
RETIRING = "retiring"
RETIRED = "retired"

ALL_STATES = frozenset({READY, ACTIVE, DRAINING, RETIRING, RETIRED})

# Legal transitions: (from, to)
_LEGAL_TRANSITIONS = frozenset({
    (READY, ACTIVE),
    (ACTIVE, DRAINING),
    (DRAINING, RETIRING),
    (RETIRING, RETIRED),
})


def validate_transition(current: str, next_state: str) -> None:
    """Raise ValueError if current -> next_state is not a legal FSM transition."""
    if current not in ALL_STATES:
        raise ValueError(f"unknown lifecycle state: {current!r}")
    if next_state not in ALL_STATES:
        raise ValueError(f"unknown lifecycle state: {next_state!r}")
    if current == next_state:
        return
    if (current, next_state) not in _LEGAL_TRANSITIONS:
        raise ValueError(
            f"illegal lifecycle transition: {current!r} -> {next_state!r}; "
            f"allowed: {sorted(_LEGAL_TRANSITIONS)}")
