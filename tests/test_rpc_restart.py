import threading
from daemon.events import EventQueue
from daemon.rpc_server import make_server
from daemon.transport import Transport
from daemon.manager import RestartOutcome
from test_rpc_server import _req            # reuse the existing request helper


class _RestartManager:
    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = []
        self._events = EventQueue()          # make_server reads manager._events for /wait

    def restart(self, session_id, force=False):
        self.calls.append((session_id, force))
        return self._outcome


def _serve(manager, port):
    srv = make_server(manager, Transport.tcp("127.0.0.1", port, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def test_restart_route_returns_new_session():
    mgr = _RestartManager(RestartOutcome("restarted", session_id="s-new", lineage_id="s-old",
                                         restart_count=1, max_restarts=3))
    srv, base = _serve(mgr, 8771)
    try:
        st, b = _req("POST", base + "/restart", body={"session_id": "s-old"})
        assert st == 200 and b["status"] == "restarted"
        assert b["session_id"] == "s-new" and b["lineage_id"] == "s-old"
        assert b["restarted_from"] == "s-old"
        assert mgr.calls == [("s-old", False)]
    finally:
        srv.shutdown()


def test_restart_route_budget_exhausted_is_409():
    mgr = _RestartManager(RestartOutcome("restart_budget_exhausted", restart_count=3, max_restarts=3))
    srv, base = _serve(mgr, 8772)
    try:
        st, b = _req("POST", base + "/restart", body={"session_id": "s-old"})
        assert st == 409 and b["error"] == "restart_budget_exhausted" and b["max_restarts"] == 3
    finally:
        srv.shutdown()


def test_restart_route_unknown_is_404():
    mgr = _RestartManager(RestartOutcome("unknown_session"))
    srv, base = _serve(mgr, 8773)
    try:
        st, b = _req("POST", base + "/restart", body={"session_id": "s-nope"})
        assert st == 404
    finally:
        srv.shutdown()


def test_restart_route_force_passed_through():
    mgr = _RestartManager(RestartOutcome("restarted", session_id="s-new", lineage_id="s-old",
                                         restart_count=1, max_restarts=3))
    srv, base = _serve(mgr, 8774)
    try:
        _req("POST", base + "/restart", body={"session_id": "s-old", "force": True})
        assert mgr.calls == [("s-old", True)]
    finally:
        srv.shutdown()


def test_restart_route_missing_session_id_is_400():
    mgr = _RestartManager(RestartOutcome("restarted", session_id="s-new", lineage_id="s-old",
                                         restart_count=1, max_restarts=3))
    srv, base = _serve(mgr, 8776)
    try:
        st, b = _req("POST", base + "/restart", body={})   # no session_id field
        assert st == 400 and "session_id" in b["error"]
    finally:
        srv.shutdown()


def test_restart_route_start_failed_is_409():
    mgr = _RestartManager(RestartOutcome("start_failed"))
    srv, base = _serve(mgr, 8777)
    try:
        st, b = _req("POST", base + "/restart", body={"session_id": "s-old"})
        assert st == 409 and b["error"] == "start_failed"
    finally:
        srv.shutdown()


def test_rpc_client_restart_round_trip():
    from rpc_client import RpcClient
    mgr = _RestartManager(RestartOutcome("restarted", session_id="s-new", lineage_id="s-old",
                                         restart_count=2, max_restarts=3))
    srv = make_server(mgr, Transport.tcp("127.0.0.1", 8775, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ok, body = RpcClient(Transport.tcp("127.0.0.1", 8775, "t")).restart("s-old")
        assert ok is True and body["status"] == "restarted" and body["restart_count"] == 2
        ok2, body2 = RpcClient(Transport.tcp("127.0.0.1", 8775, "t")).restart("s-old")  # same fake outcome
        assert ok2 is True
    finally:
        srv.shutdown()
