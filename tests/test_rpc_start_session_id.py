"""nelix-9a4.6 deliverable A (wire layer): POST /start threads an optional `session_id` body field
to manager.start(), and maps the manager's new SessionIdRejected/SessionIdInUse exceptions to the
stable error envelope (spec §10)."""
import json
import threading
import urllib.error
import urllib.request

from tests.conftest import EXECUTOR, OWNER
from daemon.events import EventQueue
from daemon.manager import SessionIdInUse, SessionIdRejected, StartOutcome
from daemon.rpc_server import make_server
from daemon.transport import Transport


def _req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={"X-Nelix-Token": "t"})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class FakeManager:
    def __init__(self):
        self._events = EventQueue()
        self.started_session_id = "__unset__"

    def start(self, executor, task, cwd, *, owner_id, model=None, session_id=None):
        self.started_session_id = session_id
        sid = session_id or "s1"
        return StartOutcome(session_id=sid, base_seq=0,
                            snapshot={"session_id": sid, "control_state": "busy",
                                      "task_delivery": "pending", "pending": False})


class FakeManagerRejectsSessionId:
    def __init__(self):
        self._events = EventQueue()

    def start(self, executor, task, cwd, *, owner_id, model=None, session_id=None):
        raise SessionIdRejected(f"invalid session_id: {session_id!r}")


class FakeManagerSessionIdInUse:
    def __init__(self):
        self._events = EventQueue()

    def start(self, executor, task, cwd, *, owner_id, model=None, session_id=None):
        raise SessionIdInUse(session_id)


def _serve(manager, port):
    srv = make_server(manager, Transport.tcp("127.0.0.1", port, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def test_start_without_session_id_passes_none():
    m = FakeManager()
    srv, base = _serve(m, 8901)
    try:
        st, b = _req("POST", base + "/start",
                     {"executor": EXECUTOR, "task": "hi", "cwd": "/repo", "owner_id": OWNER})
        assert st == 200
        assert m.started_session_id is None
        assert b["session_id"] == "s1"
    finally:
        srv.shutdown()


def test_start_threads_router_assigned_session_id_to_manager():
    m = FakeManager()
    srv, base = _serve(m, 8902)
    try:
        st, b = _req("POST", base + "/start",
                     {"executor": EXECUTOR, "task": "hi", "cwd": "/repo", "owner_id": OWNER,
                      "session_id": "s-" + "b" * 32})
        assert st == 200
        assert m.started_session_id == "s-" + "b" * 32
        assert b["session_id"] == "s-" + "b" * 32
    finally:
        srv.shutdown()


def test_start_session_id_rejected_returns_400_envelope():
    m = FakeManagerRejectsSessionId()
    srv, base = _serve(m, 8903)
    try:
        st, b = _req("POST", base + "/start",
                     {"executor": EXECUTOR, "task": "hi", "cwd": "/repo", "owner_id": OWNER,
                      "session_id": "../etc/passwd"})
        assert st == 400
        assert b["error"]["code"] == "invalid_session_id"
        assert b["error"]["retryable"] is False
        assert "message" in b["error"]
    finally:
        srv.shutdown()


def test_start_session_id_in_use_returns_409_envelope():
    m = FakeManagerSessionIdInUse()
    srv, base = _serve(m, 8904)
    try:
        st, b = _req("POST", base + "/start",
                     {"executor": EXECUTOR, "task": "hi", "cwd": "/repo", "owner_id": OWNER,
                      "session_id": "s-11112222"})
        assert st == 409
        assert b["error"]["code"] == "session_id_in_use"
        assert b["error"]["retryable"] is False
    finally:
        srv.shutdown()
