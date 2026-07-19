"""nelix-9a4.6 deliverable C (wire layer): GET /capabilities?sid=<session_id> (per-session, note
the query key is `sid` — verbatim per the brief, unlike every other route's `session_id`) and
GET /capabilities (no sid -> generation-level baseline). Owner-gated exactly like /status."""
import json
import threading
import urllib.error
import urllib.request

from tests.conftest import OWNER
from daemon.events import EventQueue
from daemon.protocol import RPC_PROTOCOL_VERSION
from daemon.rpc_server import make_server
from daemon.transport import Transport


class FakeManager:
    def __init__(self, per_session=None, baseline=None):
        self._events = EventQueue()
        self._per_session = per_session or {}
        self._baseline = baseline if baseline is not None else {"executors": {}}
        self.calls = []

    def capabilities(self, session_id=None, *, owner_id):
        self.calls.append((session_id, owner_id))
        if session_id is None:
            return self._baseline
        return self._per_session.get(session_id)   # None -> unknown/foreign, mirrors manager


def _serve(manager, port):
    srv = make_server(manager, Transport.tcp("127.0.0.1", port, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def _get(url):
    r = urllib.request.Request(url, headers={"X-Nelix-Token": "t"})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_capabilities_per_session_returns_the_manager_shape_plus_protocol():
    caps = {"session_id": "s-00000001", "executor": "demo", "hook_capable": True,
            "isolation_class": "host", "can_attach": False}
    m = FakeManager(per_session={"s-00000001": caps})
    srv, base = _serve(m, 8920)
    try:
        st, b = _get(base + f"/capabilities?sid=s-00000001&owner_id={OWNER}")
        assert st == 200
        assert b["session_id"] == "s-00000001"
        assert b["rpc_protocol"] == RPC_PROTOCOL_VERSION
        assert b["hook_capable"] is True
        assert "operations" not in b
        assert m.calls == [("s-00000001", OWNER)]
    finally:
        srv.shutdown()


def test_capabilities_unknown_sid_is_404_stable_envelope():
    # Valid-shaped (passes the wire-layer shape check) but not in the fake's per-session map — the
    # "unknown/foreign session" case, distinct from a bad-SHAPE sid (test_capabilities_bad_shape_sid...).
    m = FakeManager(per_session={})
    srv, base = _serve(m, 8921)
    try:
        st, b = _get(base + f"/capabilities?sid=s-00000099&owner_id={OWNER}")
        assert st == 404
        assert b["error"]["code"] == "unknown_session"
        assert b["error"]["retryable"] is False
        assert "message" in b["error"]
    finally:
        srv.shutdown()


def test_capabilities_requires_owner_id_same_as_status():
    m = FakeManager()
    srv, base = _serve(m, 8922)
    try:
        st, b = _get(base + "/capabilities?sid=s-00000001")           # no owner_id at all
        assert st == 400
        assert "owner_id" in b.get("error", "")
        assert m.calls == []                                  # never reaches the manager
    finally:
        srv.shutdown()


def test_capabilities_bad_shape_sid_is_400_before_reaching_the_manager():
    # nelix-9a4.6 review finding #3/#4: a bad-shape sid must 400 with the stable envelope BEFORE
    # the manager (and any filesystem access behind it) is ever consulted.
    m = FakeManager(per_session={})
    srv, base = _serve(m, 8926)
    try:
        st, b = _get(base + f"/capabilities?sid=../../etc/passwd&owner_id={OWNER}")
        assert st == 400
        assert b["error"]["code"] == "invalid_session_id"
        assert b["error"]["retryable"] is False
        assert m.calls == []
    finally:
        srv.shutdown()


def test_capabilities_empty_sid_is_400_not_the_global_payload():
    # nelix-9a4.6 review finding #4: parse_qs() drops a blank query value, so `sid=` used to read
    # as "sid omitted" and silently returned the 200 global baseline instead of rejecting the
    # caller's (probably-templating-bug) empty id.
    m = FakeManager(baseline={"executors": {"demo": {}}})
    srv, base = _serve(m, 8927)
    try:
        st, b = _get(base + f"/capabilities?sid=&owner_id={OWNER}")
        assert st == 400
        assert b["error"]["code"] == "invalid_session_id"
        assert "executors" not in b
        assert m.calls == []
    finally:
        srv.shutdown()


def test_capabilities_without_sid_returns_generation_baseline():
    baseline = {"executors": {"demo": {"driver": "claude", "launcher": "local",
                                       "hook_capable": True, "isolation_class": "host",
                                       "can_attach": False}}}
    m = FakeManager(baseline=baseline)
    srv, base = _serve(m, 8923)
    try:
        st, b = _get(base + f"/capabilities?owner_id={OWNER}")
        assert st == 200
        assert b["executors"]["demo"]["driver"] == "claude"
        assert b["rpc_protocol"] == RPC_PROTOCOL_VERSION
        assert m.calls == [(None, OWNER)]
    finally:
        srv.shutdown()


def test_capabilities_baseline_also_requires_owner_id():
    # Matches /status exactly: owner_id is required regardless of whether a session is named.
    m = FakeManager()
    srv, base = _serve(m, 8924)
    try:
        st, b = _get(base + "/capabilities")
        assert st == 400
        assert "owner_id" in b.get("error", "")
    finally:
        srv.shutdown()


def test_capabilities_reports_hookless_as_a_fact_not_a_fabricated_operation_code():
    # Fix pass (review): this used to assert a fabricated `operations["message"]` entry coded
    # `unsupported_by_generation` -- both reviewers confirmed that code name is a spec §8
    # CROSS-GENERATION concept, unrelated to the per-driver `hook_capable` fact, and that /message
    # never actually gated on it. The wire layer now relays whatever plain facts the manager
    # returns, with no operations map at all.
    caps = {"session_id": "s-00000002", "executor": "demo", "hook_capable": False,
            "isolation_class": "host", "can_attach": False}
    m = FakeManager(per_session={"s-00000002": caps})
    srv, base = _serve(m, 8925)
    try:
        st, b = _get(base + f"/capabilities?sid=s-00000002&owner_id={OWNER}")
        assert st == 200
        assert b["hook_capable"] is False
        assert "operations" not in b
    finally:
        srv.shutdown()
