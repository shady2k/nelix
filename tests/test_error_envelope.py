from daemon.errors import error_envelope


def test_error_envelope_shape():
    # Spec §10: "Stable machine error codes `{error:{code,message,retryable}}`." The RPC layer
    # uses this helper for the new routes' error cases (nelix-9a4.6) instead of ad hoc dicts, so
    # every one of them is byte-shape-identical.
    env = error_envelope("unknown_session", "unknown session, or not this owner's", retryable=False)
    assert env == {"error": {"code": "unknown_session",
                             "message": "unknown session, or not this owner's",
                             "retryable": False}}


def test_error_envelope_retryable_is_keyword_only_and_typed_through():
    # retryable is not defaulted/coerced: a caller states it explicitly, and it comes back exactly
    # as given (never silently flipped) — retryability is a fact about the CODE, not a guess.
    env = error_envelope("session_id_in_use", "session_id already in use", retryable=False)
    assert env["error"]["retryable"] is False
    env2 = error_envelope("some_retryable_code", "transient", retryable=True)
    assert env2["error"]["retryable"] is True
