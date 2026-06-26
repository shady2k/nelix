"""Stdlib-only wire protocol for the PTY broker: one JSON message per datagram,
optionally carrying exactly one fd via SCM_RIGHTS. Imported by both the broker
(daemon/pty_broker.py) and the daemon-side client (daemon/broker_client.py).
NO app imports — must stay importable inside the single-threaded broker."""
import array
import json
import socket
import struct

_MAXMSG = 65536
_FDSIZE = struct.calcsize("i")
_SOCK_BUF = 1 << 20   # raise SO_SND/RCVBUF so a full env fits in one datagram
                      # (macOS AF_UNIX/SOCK_DGRAM default is 2048 -> EMSGSIZE on real envs)


def _tune(sock):
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, _SOCK_BUF)
        except OSError:
            pass


def make_socketpair():
    """The boundary-preserving channel for the broker protocol: AF_UNIX/SOCK_DGRAM with
    raised buffers so a large argv/env request fits in a single datagram on both OSes."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    _tune(a); _tune(b)
    return a, b


def send_msg(sock, obj, fd=None):
    data = json.dumps(obj).encode()
    if fd is None:
        sock.sendmsg([data])
    else:
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", fd))]
        sock.sendmsg([data], anc)


def recv_msg(sock):
    fds = array.array("i")
    try:
        msg, anc, _flags, _addr = sock.recvmsg(_MAXMSG, socket.CMSG_LEN(_FDSIZE))
    except ConnectionResetError:
        raise EOFError                       # macOS: peer-closed SOCK_DGRAM -> ECONNRESET
    if not msg and not anc:
        raise EOFError                       # peer closed (Linux: empty datagram)
    for level, ctype, cdata in anc:
        if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
            usable = (len(cdata) // _FDSIZE) * _FDSIZE
            fds.frombytes(cdata[:usable])
    obj = json.loads(msg.decode())
    return obj, (fds[0] if len(fds) else None)
