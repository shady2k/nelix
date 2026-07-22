import os
import socket
import time

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


def test_a_closed_peer_never_blocks_recv_forever():
    """A closed peer must end the recv on EVERY supported platform — promptly, one way or another.

    This test used to assert a bare EOFError, and that assertion is macOS-only: closing one end of
    an AF_UNIX/SOCK_DGRAM pair wakes the other end's recvmsg with ECONNRESET there, which recv_msg
    translates. Linux delivers NO wakeup, so the old test did not fail on Linux — it hung, forever,
    and with --dist=loadscope it parked the entire suite at 96% until CI's cap killed the job. A
    test that hangs is worse than one that fails: it reports nothing and costs the whole run.

    So the portable contract is a deadline, not peer-close. What matters is that the wait ENDS and
    the caller learns the peer is gone; whether that arrives as EOFError (macOS, via ECONNRESET) or
    TimeoutError (Linux, via the deadline) is a platform detail, and both route to the same
    restart-and-retry in broker_client.spawn(), since socket.timeout IS TimeoutError IS an OSError.
    """
    a, b = _pair()
    b.settimeout(0.5)
    a.close()
    started = time.monotonic()
    with pytest.raises((EOFError, TimeoutError)):
        recv_msg(b)
    assert time.monotonic() - started < 5, "the recv did not honour its deadline"
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
