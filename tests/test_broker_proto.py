import os
import socket

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
