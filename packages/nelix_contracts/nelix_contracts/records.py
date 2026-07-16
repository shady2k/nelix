"""Durable record schemas. Pure: dataclasses + (de)serialisation, no I/O, no clock.

This is the ON-DISK contract. Generations eliminate live-state compatibility; they do NOT
eliminate durable-data schema compatibility (design §5) — an older generation and a newer
one read the same store. So: the version travels with every record, and reading a FUTURE
version fails closed.
"""
from dataclasses import asdict, dataclass

from .errors import INVALID_REQUEST, OWNER_MISMATCH, SCHEMA_TOO_NEW, NelixError
from .ids import (
    InvalidId, validate_generation_id, validate_orchestration_id, validate_owner_id,
    validate_session_id,
)

SCHEMA_VERSION = 1


def _validate_common(d):
    try:
        validate_session_id(d["session_id"])
        validate_owner_id(d["owner_id"])
        validate_orchestration_id(d["orchestration_id"])
        validate_generation_id(d["generation_id"])
    except (InvalidId, KeyError, TypeError) as e:
        raise NelixError(INVALID_REQUEST, f"malformed record: {e}") from None


def _check_version(d):
    version = d.get("schema_version")
    if not isinstance(version, int):
        raise NelixError(INVALID_REQUEST, "record has no schema_version")
    if version > SCHEMA_VERSION:
        raise NelixError(SCHEMA_TOO_NEW,
                         f"record schema {version} is newer than this build supports "
                         f"({SCHEMA_VERSION}); refusing to misread it")


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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionRecord":
        _check_version(d)
        _validate_common(d)
        try:
            return cls(**d)
        except TypeError as e:
            raise NelixError(INVALID_REQUEST, f"malformed session record: {e}") from None


@dataclass(frozen=True)
class TerminalRecord:
    session_id: str
    owner_id: str
    orchestration_id: str
    generation_id: str
    terminal_kind: str
    summary: str
    ended_at: float
    acknowledged_at: float | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TerminalRecord":
        _check_version(d)
        _validate_common(d)
        try:
            return cls(**d)
        except TypeError as e:
            raise NelixError(INVALID_REQUEST, f"malformed terminal record: {e}") from None


def assert_owner(record, owner_id: str) -> None:
    """The guard behind EVERY caller-facing route — reads included (design §7).

    `dialog` reads a transcript off disk and `screen` queries the live manager, so knowing a
    session id must never be sufficient to reach either. NOTE this is a CORRECTNESS
    namespace, not authentication: all local callers share one uid and can assert any owner.
    """
    if record.owner_id != owner_id:
        raise NelixError(OWNER_MISMATCH, "session belongs to another owner")
