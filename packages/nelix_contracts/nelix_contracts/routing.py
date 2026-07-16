"""The four operation classes. Pure.

This table is what keeps the router STABLE (design §1): the router dispatches on an
operation's CLASS and never on its meaning. Adding an operation is one line here; adding a
CLASS is a router change. Owner and orchestration stay opaque filter keys throughout.
"""

ACTIVE_GENERATION = "active-generation"   # start / new-session work
SESSION_KEYED = "session-keyed"           # resolved through the session's owning generation
FAN_OUT = "fan-out"                       # asked of every generation, merged by the router
OPERATOR = "operator"                     # router-local lifecycle

ALL_CLASSES = frozenset({ACTIVE_GENERATION, SESSION_KEYED, FAN_OUT, OPERATOR})

OPERATION_CLASS = {
    "start": ACTIVE_GENERATION,
    "respond": SESSION_KEYED,
    "stop": SESSION_KEYED,
    # `restart` is SESSION_KEYED because the router must FIND the session first. Note the
    # handler's split (design §5): restarting a LIVE session acts on its own generation, but
    # restarting a TERMINAL one mints a NEW session on the ACTIVE generation, inheriting the
    # stored owner server-side. That is a handler decision, not a routing class.
    "restart": SESSION_KEYED,
    "screen": SESSION_KEYED,
    "dialog": SESSION_KEYED,
    "ack_terminal": SESSION_KEYED,
    # The executor-facing plane. These route by session like any other session-keyed call,
    # but they authenticate by PER-SESSION SECRET, not by owner_id — a worker is not a
    # caller. Routing them here does not make them owner-gated.
    "hook": SESSION_KEYED,
    "message": SESSION_KEYED,
    "status": FAN_OUT,
    "wait": FAN_OUT,
    "generation_install": OPERATOR,
    "generation_activate": OPERATOR,
    "generation_retire": OPERATOR,
    "generation_list": OPERATOR,
    # Router-local: it answers from its own registry. Fanning it out would merge N
    # generations' answers into one, defeating per-session capability checks.
    "capabilities": OPERATOR,
}


class UnknownOperation(KeyError):
    """An operation with no declared class. Fail closed rather than guess a route."""


def classify(operation: str) -> str:
    try:
        return OPERATION_CLASS[operation]
    except KeyError:
        raise UnknownOperation(operation) from None
