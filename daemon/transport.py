"""Single source of truth for an RPC endpoint binding, shared by the daemon server, the client,
the supervisor (discovery), app.py and bin/nelix-wait — so transport choice never drifts.

local profile -> AF_UNIX socket, NO token (peer-gated by fs perms + peercred).
docker profile -> TCP + token (a credential must cross the container boundary)."""
import socket
import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class Transport:
    kind: str                       # "unix" | "tcp"
    path: str | None = None         # unix
    host: str | None = None         # tcp
    port: int | None = None         # tcp
    token: str | None = None        # tcp only

    @staticmethod
    def unix(path):
        return Transport(kind="unix", path=str(path))

    @staticmethod
    def tcp(host, port, token):
        return Transport(kind="tcp", host=host, port=int(port), token=token)

    def to_state(self):
        if self.kind == "unix":
            return {"transport": "unix", "path": self.path}
        return {"transport": "tcp", "host": self.host, "port": self.port, "token": self.token}

    @staticmethod
    def from_state(d):
        kind = (d or {}).get("transport")
        if kind == "unix":
            return Transport.unix(d["path"])
        if kind == "tcp":
            return Transport.tcp(d["host"], d["port"], d["token"])
        raise ValueError(f"unknown or missing transport in state: {kind!r}")
