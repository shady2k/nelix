import os
import socket

import pytest

import daemon.broker_proto as broker_proto
from daemon.broker_proto import send_msg, recv_msg


def _pair():
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)


def test_roundtrip_obj_no_fd():
    a, b = _pair()
    send_msg(a, {"v": 1, "hello": "world"})
    obj, fd = recv_msg(b)
    assert obj == {"v": 1, "hello": "world"}
    assert fd is None
    a.close(); b.close()


def test_roundtrip_with_fd():
    a, b = _pair()
    r, w = os.pipe()
    send_msg(a, {"v": 1, "status": "ok"}, fd=r)
    obj, fd = recv_msg(b)
    assert obj["status"] == "ok"
    assert fd is not None and fd != r          # a DUP of r, different number
    os.write(w, b"ping")
    assert os.read(fd, 4) == b"ping"           # the received fd refers to the same pipe
    for x in (r, w, fd):
        os.close(x)
    a.close(); b.close()


def test_eof_raises():
    a, b = _pair()
    a.close()
    try:
        recv_msg(b)
        assert False, "expected EOFError"
    except EOFError:
        pass
    b.close()


def test_oversized_datagram_truncation_raises(monkeypatch):
    # A datagram larger than the recv cap must be rejected, not silently truncated
    # into corrupt JSON (which would otherwise crash json.loads in the broker).
    a, b = _pair()
    try:
        monkeypatch.setattr(broker_proto, "_MAXMSG", 16)
        send_msg(a, {"v": 1, "payload": "x" * 1000})   # well over the 16-byte cap
        # Must be the explicit truncation guard, not a coincidental JSONDecodeError on the
        # truncated bytes — truncated data can also parse as valid-but-corrupt JSON.
        with pytest.raises(ValueError, match="datagram truncated"):
            recv_msg(b)
    finally:
        a.close(); b.close()
