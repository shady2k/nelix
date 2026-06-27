import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.transport import Transport  # noqa: E402


def test_unix_roundtrips_through_state_without_a_token():
    t = Transport.unix("/run/nelix/rpc.sock")
    assert t.to_state() == {"transport": "unix", "path": "/run/nelix/rpc.sock"}
    assert Transport.from_state(t.to_state()) == t
    assert t.token is None


def test_tcp_roundtrips_through_state_with_a_token():
    t = Transport.tcp("127.0.0.1", 54321, "deadbeef")
    assert t.to_state() == {"transport": "tcp", "host": "127.0.0.1",
                            "port": 54321, "token": "deadbeef"}
    assert Transport.from_state(t.to_state()) == t


def test_from_state_rejects_unknown_transport():
    import pytest
    with pytest.raises(ValueError):
        Transport.from_state({"transport": "carrier-pigeon"})
    with pytest.raises(ValueError):
        Transport.from_state({"pid": 1})        # legacy {pid,port,token} has no transport
