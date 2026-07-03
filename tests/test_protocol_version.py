from daemon.protocol import RPC_PROTOCOL_VERSION


def test_rpc_protocol_version_is_4():
    # Bumped 3 -> 4 for nelix-g9k: a new additive route GET /models. A daemon left on stale code
    # would answer /models with a bare 404 (route absent) — the bump forces the supervisor to
    # recycle it instead, so the route is actually present before a caller relies on it.
    assert RPC_PROTOCOL_VERSION == 4
