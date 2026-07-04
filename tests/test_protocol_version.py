from daemon.protocol import RPC_PROTOCOL_VERSION


def test_rpc_protocol_version_is_5():
    # Bumped 4 -> 5 for nelix-kwr: /start gained a new rejection shape, ModelUnavailable -> 400
    # {"error", "available_models"}. A daemon left on stale code would answer with the old
    # generic 409 (no available_models) — the bump forces the supervisor to recycle it instead,
    # so the new response shape is actually present before a caller relies on it.
    assert RPC_PROTOCOL_VERSION == 5
