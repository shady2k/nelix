"""nelix-9a4.6 deliverable B: GET /health — spec §8/§10, a liveness/identity probe. No owner_id,
no session id required (unlike every other caller-facing route)."""
import json
import threading
import urllib.error
import urllib.request

from daemon.events import EventQueue
from daemon.protocol import RPC_PROTOCOL_VERSION
from tests.conftest import serve


class _Manager:
    def __init__(self):
        self._events = EventQueue()


def _get(url):
    r = urllib.request.Request(url, headers={"X-Nelix-Token": "t"})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_health_returns_ok_with_protocol_and_generation_id():
    srv, base = serve(_Manager())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _get(base + "/health")
        assert st == 200
        assert b["status"] == "ok"
        assert b["rpc_protocol"] == RPC_PROTOCOL_VERSION
        assert "generation_id" in b            # None in this dev/test environment
        assert "generation_epoch" in b          # None in the singleton path
        assert "build_id" in b                  # None in dev checkout (not an installed runtime)
    finally:
        srv.shutdown()


def test_health_requires_no_owner_id_and_no_session_id():
    # Unlike /status, /screen, /dialog, /wait: a liveness probe must not need a caller identity.
    srv, base = serve(_Manager())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _get(base + "/health")
        assert st == 200 and "error" not in b
    finally:
        srv.shutdown()


def test_health_still_requires_transport_auth():
    # The route is unauthenticated w.r.t. owner_id, but transport auth (token/peercred) still
    # applies — a health probe is not a hole in the auth boundary.
    srv, base = serve(_Manager())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = urllib.request.Request(base + "/health")   # no X-Nelix-Token
        try:
            urllib.request.urlopen(r, timeout=5)
            assert False, "expected 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        srv.shutdown()
