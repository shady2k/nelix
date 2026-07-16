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


def test_every_code_declares_retryability():
    for code in ALL_CODES:
        assert isinstance(NelixError(code, "m").retryable, bool)


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
