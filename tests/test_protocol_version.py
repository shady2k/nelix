from daemon.protocol import RPC_PROTOCOL_VERSION


def test_rpc_protocol_version_is_7():
    # Bumped 6 -> 7 (nelix-9a4.6): the additive arrival of GET /health, GET /capabilities, and
    # POST /start's optional `session_id` (spec §3/§8/§10). A stale daemon lacking these would
    # 404 (health/capabilities) or silently ignore the id (start) rather than honor the new
    # contract — the bump forces the supervisor to recycle it so the new routes are present.
    assert RPC_PROTOCOL_VERSION == 7
