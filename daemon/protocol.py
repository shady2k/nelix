"""RPC wire-protocol version, shared by the daemon's HTTP server and the in-process supervisor.

Bump RPC_PROTOCOL_VERSION whenever the /start, /respond, /status, /stop or /restart request or
response shapes change. The supervisor stamps it into /status and refuses to reuse (or adopt) a
daemon whose /status reports a different — or missing — version. That is how a daemon left running
on stale code after a plugin update is detected and recycled, instead of being spoken to with a
mismatched protocol (the failure mode: a half-understood request the old daemon closes mid-reply,
surfacing to the caller as RemoteDisconnected).

No project imports here (stdlib-free, in fact) so both the package-mode in-process plugin and the
top-level-module daemon resolve the identical constant.
"""

RPC_PROTOCOL_VERSION = 3
