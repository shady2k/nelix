from daemon.protocol import RPC_PROTOCOL_VERSION


def test_rpc_protocol_version_is_3():
    # Bumped 2 -> 3 for nelix-9k0: the /start request shape gained an optional `model` field. A
    # daemon left on stale code would silently IGNORE `model` (run the default model, no error) —
    # a non-benign silent failure — so the version bump forces the supervisor to recycle it instead.
    assert RPC_PROTOCOL_VERSION == 3
