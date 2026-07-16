"""Stable machine error codes and the response envelope. Pure.

Callers branch on `code`, NEVER on `message` (design §10). Adding a code is additive;
changing a spelling is a breaking change to the contract.
"""

OWNER_MISMATCH = "owner_mismatch"
UNKNOWN_SESSION = "unknown_session"
CURSOR_EXPIRED = "cursor_expired"
BOARD_CHANGED = "board_changed"
BOARD_INCOMPLETE = "board_incomplete"
UNSUPPORTED_BY_GENERATION = "unsupported_by_generation"
CONCURRENCY_LIMIT = "concurrency_limit"
DUPLICATE_START = "duplicate_start"
GENERATION_UNAVAILABLE = "generation_unavailable"
ORPHAN_REAPED = "orphan_reaped"
INVALID_REQUEST = "invalid_request"
SCHEMA_TOO_NEW = "schema_too_new"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"
STORE_CORRUPT = "store_corrupt"
STORE_UNAVAILABLE = "store_unavailable"

# retryable=True means: the SAME call, unchanged, may succeed later.
# It is deliberately False for the cursor conditions — they mean "refetch the board and
# re-arm", so a verbatim retry would spin.
_RETRYABLE = {
    BOARD_INCOMPLETE: True,
    GENERATION_UNAVAILABLE: True,
    CONCURRENCY_LIMIT: True,
    OWNER_MISMATCH: False,
    UNKNOWN_SESSION: False,
    CURSOR_EXPIRED: False,
    BOARD_CHANGED: False,
    UNSUPPORTED_BY_GENERATION: False,
    DUPLICATE_START: False,
    ORPHAN_REAPED: False,
    INVALID_REQUEST: False,
    SCHEMA_TOO_NEW: False,
    IDEMPOTENCY_CONFLICT: False,
    STORE_CORRUPT: False,
    STORE_UNAVAILABLE: True,     # busy / lock contention / environment: the same call may work later
}

ALL_CODES = frozenset(_RETRYABLE)


class NelixError(Exception):
    """A contract error. `code` is the API surface; `message` is for humans only."""

    def __init__(self, code: str, message: str):
        if code not in _RETRYABLE:
            raise ValueError(f"unknown error code: {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def retryable(self) -> bool:
        return _RETRYABLE[self.code]

    def to_envelope(self) -> dict:
        return {"error": {"code": self.code, "message": self.message,
                          "retryable": self.retryable}}
