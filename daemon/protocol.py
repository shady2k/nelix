"""RPC wire-protocol version, shared by the daemon's HTTP server and the in-process supervisor.

Bump RPC_PROTOCOL_VERSION whenever the wire contract changes: the /start, /respond, /status, /stop
or /restart request/response shapes, OR the ADDITIVE arrival of a new route (e.g. /dialog). The
supervisor stamps it into /status and refuses to reuse (or adopt) a daemon whose /status reports a
different — or missing — version. That is how a daemon left running on stale code after a plugin
update is detected and recycled, instead of being spoken to with a mismatched protocol. For a
shape change the failure mode is a half-understood request the old daemon closes mid-reply
(RemoteDisconnected); for a new route it is a bare 404 from a stale daemon that lacks it — the
bump forces a recycle so the new route is actually present.

No project imports here (stdlib-free, in fact) so both the package-mode in-process plugin and the
top-level-module daemon resolve the identical constant.
"""

RPC_PROTOCOL_VERSION = 7
# Bumped 6 -> 7 (nelix-9a4.6): the additive arrival of GET /health, GET /capabilities, and POST
# /start's optional `session_id` (spec §3/§8/§10 — the generation's side of the future
# router<->generation contract). A stale daemon lacking these would 404 (health/capabilities) or
# silently ignore the id (start) rather than honor the new contract, so the bump forces a recycle
# per this module's own rule for "the ADDITIVE arrival of a new route".
