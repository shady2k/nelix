"""Single source of truth for an RPC endpoint binding, shared by the daemon server, the client,
the supervisor (discovery), app.py and bin/nelix-wait — so transport choice never drifts.

local profile -> AF_UNIX socket, NO token (peer-gated by fs perms + peercred).
docker profile -> TCP + token (a credential must cross the container boundary)."""
import os
import socket
import struct
import sys
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


def peer_uid(sock):
    """The uid of the process on the other end of a unix-domain `sock`, or None if the platform
    cannot report it. Linux: SO_PEERCRED (struct ucred = pid,uid,gid). macOS/BSD: LOCAL_PEERCRED
    (struct xucred; cr_uid is the 2nd 32-bit field after cr_version)."""
    try:
        if sys.platform.startswith("linux"):
            # struct ucred { pid_t pid; uid_t uid; gid_t gid; } — unpacked as 3 signed int32 (real uids < 2^31; fail-closed)
            buf = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", buf)
            return uid
        if sys.platform == "darwin":
            SOL_LOCAL = 0
            LOCAL_PEERCRED = 0x0001
            # struct xucred { u_int cr_version; uid_t cr_uid; short cr_ngroups; uid_t cr_groups[16]; }
            buf = sock.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, struct.calcsize("2I"))
            _version, uid = struct.unpack("2I", buf)
            return uid
    except OSError:
        return None
    return None


def peer_is_self(sock):
    """Defense-in-depth on top of the 0700 dir / 0600 socket: reject only a KNOWN foreign uid.
    Unknown (platform can't report) -> allow, because fs perms are the real local boundary."""
    uid = peer_uid(sock)
    return uid is None or uid == os.getuid()
