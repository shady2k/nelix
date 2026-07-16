import pytest

from nelix_contracts import errors
from nelix_contracts.errors import ALL_CODES, NelixError


def test_envelope_shape_is_the_contract():
    err = NelixError(errors.OWNER_MISMATCH, "session belongs to another owner")
    assert err.to_envelope() == {
        "error": {"code": "owner_mismatch",
                  "message": "session belongs to another owner",
                  "retryable": False}
    }


def test_unknown_code_is_rejected_at_construction():
    # A typo'd code must blow up here, not reach a caller who branches on it.
    with pytest.raises(ValueError):
        NelixError("whoops_not_a_code", "x")


def test_every_public_code_constant_declares_retryability():
    # Scan the MODULE's string constants, not ALL_CODES — ALL_CODES is derived from the same
    # dict this is meant to check, so iterating it can never catch a missing entry.
    declared = {v for k, v in vars(errors).items()
                if k.isupper() and not k.startswith("_") and isinstance(v, str)}
    assert declared == set(ALL_CODES), (
        f"code constants without a retryability entry: {declared - set(ALL_CODES)}; "
        f"entries without a constant: {set(ALL_CODES) - declared}")


def test_idempotency_conflict_is_distinct_from_duplicate_start():
    # A replay of the SAME request is a success. A replay of the same KEY with a DIFFERENT
    # request is a conflict. They are different conditions and must not share a code.
    assert errors.IDEMPOTENCY_CONFLICT != errors.DUPLICATE_START
    assert NelixError(errors.IDEMPOTENCY_CONFLICT, "m").retryable is False


def test_store_corrupt_does_not_blame_the_caller():
    assert NelixError(errors.STORE_CORRUPT, "m").retryable is False


def test_transient_backend_conditions_are_retryable():
    # The caller may repeat the SAME call unchanged and expect it to work later.
    assert NelixError(errors.BOARD_INCOMPLETE, "m").retryable is True
    assert NelixError(errors.GENERATION_UNAVAILABLE, "m").retryable is True
    assert NelixError(errors.CONCURRENCY_LIMIT, "m").retryable is True


def test_cursor_conditions_are_not_retryable():
    # cursor_expired/board_changed mean "refetch the board and re-arm" — repeating the same
    # call with the same cursor would loop forever.
    assert NelixError(errors.CURSOR_EXPIRED, "m").retryable is False
    assert NelixError(errors.BOARD_CHANGED, "m").retryable is False


def test_nelix_error_is_an_exception_carrying_its_message():
    with pytest.raises(NelixError) as ei:
        raise NelixError(errors.UNKNOWN_SESSION, "no such session")
    assert str(ei.value) == "no such session"
    assert ei.value.code == "unknown_session"


def test_store_unavailable_is_retryable_and_distinct_from_corrupt():
    # Busy is not broken. STORE_CORRUPT is non-retryable and means "your durable state is
    # damaged"; a caller that merely lost a lock race must back off and retry, not escalate.
    assert errors.STORE_UNAVAILABLE != errors.STORE_CORRUPT
    assert NelixError(errors.STORE_UNAVAILABLE, "m").retryable is True
    assert NelixError(errors.STORE_CORRUPT, "m").retryable is False
