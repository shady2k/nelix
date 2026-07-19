import threading
from daemon.events import EventQueue
from daemon.rpc_server import make_server
from daemon.transport import Transport
from daemon.manager import RestartOutcome
from tests.test_rpc_server import _req            # reuse the existing request helper
from tests.conftest import OWNER


class _RestartManager:
    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = []
        self._events = EventQueue()          # make_server reads manager._events for /wait

    def restart(self, session_id, *, owner_id, new_session_id, force=False):
        self.calls.append((session_id, force))
        return self._outcome


def _serve(manager, port):
    srv = make_server(manager, Transport.tcp("127.0.0.1", port, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def test_restart_route_returns_new_session():
    mgr = _RestartManager(RestartOutcome("restarted", session_id="s-00000000000000000000000000000001",
                                         lineage_id="s-old0000000000000000000000000000",
                                         restart_count=1, max_restarts=3,
                                         snapshot={"session_id": "s-00000000000000000000000000000001",
                                                   "control_state": "busy"}))
    srv, base = _serve(mgr, 8771)
    try:
        st, b = _req("POST", base + "/restart", body={"session_id": "s-0123456789abcdef0123456789abcdef",
                       "new_session_id": "s-00000000000000000000000000000001",
                       "owner_id": OWNER})
        assert st == 200 and b["operation"] == "restart" and b["status"] == "restarted"
        assert b["session_id"] == "s-00000000000000000000000000000001"
        assert b["lineage_id"] == "s-old0000000000000000000000000000"
        assert b["restarted_from"] == "s-0123456789abcdef0123456789abcdef" and b["next_action"] == "end_turn"
        assert mgr.calls == [("s-0123456789abcdef0123456789abcdef", False)]
    finally:
        srv.shutdown()


def test_restart_route_budget_exhausted_is_409():
    mgr = _RestartManager(RestartOutcome("restart_budget_exhausted", restart_count=3, max_restarts=3))
    srv, base = _serve(mgr, 8772)
    try:
        st, b = _req("POST", base + "/restart", body={"session_id": "s-0123456789abcdef0123456789abcdef",
                       "new_session_id": "s-00000000000000000000000000000002",
                       "owner_id": OWNER})
        assert st == 409 and b["error"] == "restart_budget_exhausted" and b["max_restarts"] == 3
    finally:
        srv.shutdown()


def test_restart_route_unknown_is_404():
    mgr = _RestartManager(RestartOutcome("unknown_session"))
    srv, base = _serve(mgr, 8773)
    try:
        st, b = _req("POST", base + "/restart", body={"session_id": "s-dead0000000000000000000000000000",
                       "new_session_id": "s-00000000000000000000000000000003",
                       "owner_id": OWNER})
        assert st == 404
    finally:
        srv.shutdown()


def test_restart_route_force_passed_through():
    mgr = _RestartManager(RestartOutcome("restarted", session_id="s-00000000000000000000000000000001",
                                         lineage_id="s-old0000000000000000000000000000",
                                         restart_count=1, max_restarts=3,
                                         snapshot={"session_id": "s-00000000000000000000000000000001",
                                                   "control_state": "busy"}))
    srv, base = _serve(mgr, 8774)
    try:
        _req("POST", base + "/restart", body={"session_id": "s-0123456789abcdef0123456789abcdef",
               "force": True,
               "new_session_id": "s-00000000000000000000000000000004",
               "owner_id": OWNER})
        assert mgr.calls == [("s-0123456789abcdef0123456789abcdef", True)]
    finally:
        srv.shutdown()


def test_restart_route_missing_session_id_is_400():
    mgr = _RestartManager(RestartOutcome("restarted", session_id="s-00000000000000000000000000000001",
                                         lineage_id="s-old0000000000000000000000000000",
                                         restart_count=1, max_restarts=3))
    srv, base = _serve(mgr, 8776)
    try:
        # owner + new_session_id supplied; session_id absent: keeps this test about the MISSING
        # session_id it is named for. (An owner-less body is refused first — that is covered by
        # test_owner_isolation.py::test_restart_requires_an_owner.)
        st, b = _req("POST", base + "/restart", body={"owner_id": OWNER,
                       "new_session_id": "s-00000000000000000000000000000005"})   # no session_id field
        assert st == 400 and "session_id" in b["error"]
    finally:
        srv.shutdown()


def test_restart_route_start_failed_is_409():
    mgr = _RestartManager(RestartOutcome("start_failed"))
    srv, base = _serve(mgr, 8777)
    try:
        st, b = _req("POST", base + "/restart", body={"session_id": "s-0123456789abcdef0123456789abcdef",
                       "new_session_id": "s-00000000000000000000000000000006",
                       "owner_id": OWNER})
        assert st == 409 and b["error"] == "start_failed"
    finally:
        srv.shutdown()


def test_restart_route_includes_next_after_seq():
    # The /restart 200 body must carry next_after_seq so the plugin arms the restarted session's
    # waiter at the daemon-reported base cursor (symmetric with /start).
    mgr = _RestartManager(RestartOutcome("restarted", session_id="s-00000000000000000000000000000001",
                                         lineage_id="lin000000000000000000000000000000",
                                         restart_count=1, max_restarts=3, next_after_seq=42,
                                         snapshot={"session_id": "s-00000000000000000000000000000001",
                                                   "control_state": "busy"}))
    srv, base = _serve(mgr, 8778)
    try:
        st, b = _req("POST", base + "/restart", body={"session_id": "s-0123456789abcdef0123456789abcdef",
                       "new_session_id": "s-00000000000000000000000000000007",
                       "owner_id": OWNER})
        assert st == 200 and b["next_after_seq"] == 42
    finally:
        srv.shutdown()


def test_rpc_client_restart_round_trip():
    # Uses the HTTP layer directly (via _req) so new_session_id is in the body. RpcClient.restart
    # also needs new_session_id; this test exercises the server round-trip with a valid body.
    mgr = _RestartManager(RestartOutcome("restarted", session_id="s-00000000000000000000000000000001",
                                         lineage_id="s-old0000000000000000000000000000",
                                         restart_count=2, max_restarts=3,
                                         snapshot={"session_id": "s-00000000000000000000000000000001",
                                                   "control_state": "busy"}))
    srv, base = _serve(mgr, 8775)
    try:
        st, body = _req("POST", base + "/restart", body={"session_id": "s-0123456789abcdef0123456789abcdef",
                          "new_session_id": "s-00000000000000000000000000000008",
                          "owner_id": OWNER})
        assert st == 200 and body["status"] == "restarted" and body["restart_count"] == 2
        st2, body2 = _req("POST", base + "/restart", body={"session_id": "s-0123456789abcdef0123456789abcdef",
                           "new_session_id": "s-00000000000000000000000000000009",
                           "owner_id": OWNER})
        assert st2 == 200
    finally:
        srv.shutdown()
