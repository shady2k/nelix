import http.client
import json
import threading
from urllib.parse import urlparse

import pytest

from daemon.rpc_server import make_server
from daemon.transport import Transport


class FakeSession:
    """Stands in for a real Session (added in Task 8): exposes a per-session hook_secret and
    records every HookEvent handed to on_hook."""

    def __init__(self, secret="sek"):
        self.hook_secret = secret
        self.hooks = []

    def on_hook(self, ev):
        self.hooks.append(ev)


class FakeManager:
    def __init__(self, session):
        self._session = session

    def get(self, sid):
        return self._session if sid == "s-11111111" else None


@pytest.fixture
def server():
    session = FakeSession()
    srv = make_server(FakeManager(session), Transport.tcp("127.0.0.1", 0, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, session
    srv.shutdown()


def _post(base, path, *, body=None, raw=None, headers=None, content_length=None):
    u = urlparse(base)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    hdrs = {"X-Nelix-Token": "t"}
    if headers:
        hdrs.update(headers)
    if content_length is not None:
        # Claim a large Content-Length but send NO body — exercises the pre-read cap check
        # without deadlocking on the socket send buffer.
        c.putrequest("POST", path)
        for k, v in hdrs.items():
            c.putheader(k, v)
        c.putheader("Content-Length", str(content_length))
        c.endheaders()
    else:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        c.request("POST", path, body=data, headers=hdrs)
    r = c.getresponse()
    st = r.status
    r.read()
    c.close()
    return st


def test_hook_accepted_with_secret(server):
    base, session = server
    st = _post(base, "/hook/s-11111111", body={"hook_event_name": "Stop"},
               headers={"X-Nelix-Hook-Secret": "sek"})
    assert st == 204
    assert session.hooks[-1].event == "Stop"
    assert session.hooks[-1].session_id == "s-11111111"


def test_hook_parses_optional_fields(server):
    base, session = server
    st = _post(base, "/hook/s-11111111",
               body={"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                     "tool_input": {"question": "JSON or YAML?"}, "is_interrupt": True,
                     "message": "permission_prompt"},
               headers={"X-Nelix-Hook-Secret": "sek"})
    assert st == 204
    ev = session.hooks[-1]
    assert ev.event == "PreToolUse" and ev.tool_name == "AskUserQuestion"
    assert ev.tool_input == {"question": "JSON or YAML?"}
    assert ev.is_interrupt is True
    assert ev.notification == "permission_prompt"


def test_hook_rejected_wrong_secret(server):
    base, session = server
    st = _post(base, "/hook/s-11111111", body={"hook_event_name": "Stop"},
               headers={"X-Nelix-Hook-Secret": "nope"})
    assert st == 401
    assert session.hooks == []


def test_hook_missing_secret_401(server):
    base, session = server
    st = _post(base, "/hook/s-11111111", body={"hook_event_name": "Stop"})
    assert st == 401
    assert session.hooks == []


def test_hook_unknown_session_401(server):
    base, session = server
    st = _post(base, "/hook/s-deadbeef", body={"hook_event_name": "Stop"},
               headers={"X-Nelix-Hook-Secret": "sek"})
    assert st == 401
    assert session.hooks == []


def test_hook_body_cap_413(server):
    base, _ = server
    st = _post(base, "/hook/s-11111111", headers={"X-Nelix-Hook-Secret": "sek"},
               content_length=256 * 1024 + 1)
    assert st == 413


def test_hook_malformed_json_400(server):
    base, _ = server
    st = _post(base, "/hook/s-11111111", raw=b"{not json",
               headers={"X-Nelix-Hook-Secret": "sek"})
    assert st == 400


def test_hook_rate_limit_drops_flood(server):
    # MINOR (spec §7: rate-limit alongside the body cap): a per-session flood must be dropped. A sane
    # single hook is accepted (204); a burst well past the per-session bucket yields 429s (dropped),
    # so a same-uid process cannot flood forged lifecycle events unbounded.
    base, session = server
    hdr = {"X-Nelix-Hook-Secret": "sek"}
    codes = [_post(base, "/hook/s-11111111", body={"hook_event_name": "PostToolUse"}, headers=hdr)
             for _ in range(400)]
    assert codes[0] == 204                                # a normal single hook is accepted
    assert 429 in codes                                  # the flood is rate-limited (dropped)
    assert len(session.hooks) < 400                      # dropped hooks never reached on_hook
