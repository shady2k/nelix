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

RPC_PROTOCOL_VERSION = 5
