"""Identifier minting and validation. Pure: stdlib only, no I/O, no clock.

Ids are OPAQUE to the router: it routes on them without knowing what they mean (design
§1 — "owner and orchestration stay opaque routing/filter keys"). Nothing here may branch
on an id's meaning.
"""
import re
import uuid

_HEX32 = "[0-9a-f]{32}"
_SESSION_RE = re.compile(rf"^s-{_HEX32}$")
_ORCH_RE = re.compile(rf"^o-{_HEX32}$")
_GENERATION_RE = re.compile(rf"^g-{_HEX32}$")
# An owner is caller-supplied and DURABLE (a profile/installation identity, not a pid or a
# conversation), so we constrain its charset rather than mint it. Leading char is alnum so a
# value can never be mistaken for a flag.
_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class InvalidId(ValueError):
    """A malformed identifier. Fail closed: never coerce, never default."""


def new_session_id() -> str:
    # FULL uuid4 (128 bits), not uuid4().hex[:8]. The namespace is long-lived, spans
    # generations, and retains archived sessions; 32 bits is not enough.
    return "s-" + uuid.uuid4().hex


def new_orchestration_id() -> str:
    return "o-" + uuid.uuid4().hex


def new_generation_id() -> str:
    return "g-" + uuid.uuid4().hex


def _check(value, rx, kind):
    if not isinstance(value, str) or rx.match(value) is None:
        raise InvalidId(f"invalid {kind}: {value!r}")
    return value


def validate_session_id(value) -> str:
    return _check(value, _SESSION_RE, "session_id")


def validate_orchestration_id(value) -> str:
    return _check(value, _ORCH_RE, "orchestration_id")


def validate_generation_id(value) -> str:
    return _check(value, _GENERATION_RE, "generation_id")


def validate_owner_id(value) -> str:
    return _check(value, _OWNER_RE, "owner_id")
