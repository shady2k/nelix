"""Durable record schemas. Pure: dataclasses + (de)serialisation, no I/O, no clock.

This is the ON-DISK contract. Generations eliminate live-state compatibility; they do NOT
eliminate durable-data schema compatibility (design §5) — an older generation and a newer
one read the same store. So: the version travels with every record, and reading a FUTURE
version fails closed.
"""
import math
from dataclasses import asdict, dataclass

from .errors import INVALID_REQUEST, OWNER_MISMATCH, SCHEMA_TOO_NEW, NelixError
from .ids import (
    InvalidId, validate_generation_id, validate_orchestration_id, validate_owner_id,
    validate_session_id,
)

# 3: TerminalRecord gained terminal_seq — per-generation monotonic watermark for retirement
#     (nelix-gm3). Bumped together with db.SCHEMA_VERSION.
#
# THE nelix-165 TRAP: this constant is SEPARATE from db.SCHEMA_VERSION. Both moved together
# (1→2→3) but nothing enforces that. The nelix-165 fix is still planned; for now the two
# continue to move together by convention.
SCHEMA_VERSION = 3


def _check_version(d):
    version = d.get("schema_version")
    if not isinstance(version, int):
        raise NelixError(INVALID_REQUEST, "record has no schema_version")
    if version > SCHEMA_VERSION:
        raise NelixError(SCHEMA_TOO_NEW,
                         f"record schema {version} is newer than this build supports "
                         f"({SCHEMA_VERSION}); refusing to misread it")


def _text(value, name):
    if not isinstance(value, str):
        raise NelixError(INVALID_REQUEST, f"{name} must be a string: {value!r}")
    return value


def timestamp(value, name, *, optional=False):
    """The one rule for a moment in time, on disk or in hand.

    Public because it is not only a field validator: an unchecked CLOCK read reintroduces
    exactly what it rejects — a NaN `now` makes every comparison against it False, which
    silently disables whatever the comparison was bounding.
    """
    if value is None:
        if optional:
            return None
        raise NelixError(INVALID_REQUEST, f"{name} is required")
    # bool is an int subclass — True would silently become 1.0.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NelixError(INVALID_REQUEST, f"{name} must be a number: {value!r}")
    if not math.isfinite(value):
        raise NelixError(INVALID_REQUEST, f"{name} must be finite: {value!r}")
    return float(value)


def _version(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise NelixError(INVALID_REQUEST, f"schema_version must be a positive int: {value!r}")
    if value > SCHEMA_VERSION:
        raise NelixError(SCHEMA_TOO_NEW,
                         f"record schema {value} is newer than this build supports "
                         f"({SCHEMA_VERSION}); refusing to misread it")
    return value


def _ids(session_id, owner_id, orchestration_id, generation_id):
    try:
        validate_session_id(session_id)
        validate_owner_id(owner_id)
        validate_orchestration_id(orchestration_id)
        validate_generation_id(generation_id)
    except InvalidId as e:
        raise NelixError(INVALID_REQUEST, str(e)) from None


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    owner_id: str
    orchestration_id: str
    generation_id: str
    state: str
    executor: str
    task: str
    cwd: str
    model: str | None
    created_at: float
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        _version(self.schema_version)
        _ids(self.session_id, self.owner_id, self.orchestration_id, self.generation_id)
        for name in ("state", "executor", "task", "cwd"):
            _text(getattr(self, name), name)
        if self.model is not None:
            _text(self.model, "model")
        timestamp(self.created_at, "created_at")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionRecord":
        if not isinstance(d, dict):
            raise NelixError(INVALID_REQUEST, "record must be an object")
        _check_version(d)          # SCHEMA_TOO_NEW before any field is interpreted
        try:
            return cls(**d)
        except TypeError as e:
            raise NelixError(INVALID_REQUEST, f"malformed record: {e}") from None


@dataclass(frozen=True)
class TerminalRecord:
    session_id: str
    owner_id: str
    orchestration_id: str
    generation_id: str
    terminal_kind: str
    summary: str
    ended_at: float
    published_at: float
    terminal_seq: int = 0
    acknowledged_at: float | None = None
    expired_at: float | None = None
    expire_reason: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        _version(self.schema_version)
        _ids(self.session_id, self.owner_id, self.orchestration_id, self.generation_id)
        _text(self.terminal_kind, "terminal_kind")
        _text(self.summary, "summary")
        timestamp(self.ended_at, "ended_at")
        # The store's own stamp, so it is REQUIRED: a receipt that cannot say when this package
        # published it cannot be aged by this package's policy, which is the whole point of
        # having it rather than reusing the caller's ended_at.
        timestamp(self.published_at, "published_at")
        # terminal_seq must be a non-negative, non-boolean int. 0 is valid (unset/legacy).
        if isinstance(self.terminal_seq, bool) or not isinstance(self.terminal_seq, int) \
                or self.terminal_seq < 0:
            raise NelixError(INVALID_REQUEST,
                             f"terminal_seq must be a non-negative int: {self.terminal_seq!r}")
        timestamp(self.acknowledged_at, "acknowledged_at", optional=True)
        timestamp(self.expired_at, "expired_at", optional=True)
        if self.expire_reason is not None:
            _text(self.expire_reason, "expire_reason")
        # The COMBINATIONS (expired without a reason, expired AND acknowledged, a reason outside
        # age/count) are deliberately NOT re-checked here. They are CHECK constraints on the
        # terminal table, SQLite enforces those against every writer unconditionally, and the db
        # version gate refuses any file whose schema predates them — so a record built from a
        # stored row physically cannot carry one. A copy of the rule here would be a branch no
        # test could reach: not a guard, just a second place for the rule to rot.


    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TerminalRecord":
        if not isinstance(d, dict):
            raise NelixError(INVALID_REQUEST, "record must be an object")
        _check_version(d)          # SCHEMA_TOO_NEW before any field is interpreted
        try:
            return cls(**d)
        except TypeError as e:
            raise NelixError(INVALID_REQUEST, f"malformed record: {e}") from None


def assert_owner(record, owner_id: str) -> None:
    """The guard behind EVERY caller-facing route — reads included (design §7).

    `dialog` reads a transcript off disk and `screen` queries the live manager, so knowing a
    session id must never be sufficient to reach either. NOTE this is a CORRECTNESS
    namespace, not authentication: all local callers share one uid and can assert any owner.
    """
    if record.owner_id != owner_id:
        raise NelixError(OWNER_MISMATCH, "session belongs to another owner")
