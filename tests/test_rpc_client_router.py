"""nelix-3rm (router slice 3c.1): the router assigns a session id BEFORE forwarding /start, so
RpcClient.start must thread an optional `session_id` into the POST body (additive — omitted is
byte-identical to before), and the router needs a no-owner /health probe to read a generation's
build-id."""
from conftest import EXECUTOR, OWNER
from daemon.transport import Transport
from rpc_client import RpcClient


def test_client_start_includes_session_id_when_provided(monkeypatch):
    c = RpcClient(Transport.tcp("x", 80, "t"), OWNER)
    seen = {}
    monkeypatch.setattr(c, "_call",
                        lambda m, p, body=None, **k: seen.update(body=body) or (200, {}))
    sid = "s-" + "a" * 32
    c.start(EXECUTOR, "go", "/repo", session_id=sid)
    assert seen["body"] == {"executor": EXECUTOR, "task": "go", "cwd": "/repo",
                            "owner_id": OWNER, "session_id": sid}


def test_client_start_omits_session_id_when_not_provided(monkeypatch):
    # Additive: an omitted session_id is the exact pre-feature body (mirrors model=None).
    c = RpcClient(Transport.tcp("x", 80, "t"), OWNER)
    seen = {}
    monkeypatch.setattr(c, "_call",
                        lambda m, p, body=None, **k: seen.update(body=body) or (200, {}))
    c.start(EXECUTOR, "go", "/repo")
    assert "session_id" not in seen["body"]


def test_client_health_gets_health_route(monkeypatch):
    c = RpcClient(Transport.tcp("x", 80, "t"), OWNER)
    seen = {}
    monkeypatch.setattr(c, "_call",
                        lambda m, p, body=None, **k: seen.update(m=m, p=p)
                        or (200, {"status": "ok", "generation_id": None}))
    body = c.health()
    assert body == {"status": "ok", "generation_id": None}
    assert seen == {"m": "GET", "p": "/health"}
