from daemon.protocol import RPC_PROTOCOL_VERSION


def test_rpc_protocol_version_is_6():
    # Bumped 5 -> 6 for nelix tool-clarity: /dialog with an omitted limit now returns a bounded
    # page (DEFAULT_DIALOG_PAGE_CHARS) instead of the whole transcript, and every /dialog page
    # carries at_end (+ hint at the end). A stale daemon would still stream the full transcript
    # with no at_end — the bump forces the supervisor to recycle it so the new contract is present
    # before a caller relies on bounded pages / at_end.
    assert RPC_PROTOCOL_VERSION == 6
