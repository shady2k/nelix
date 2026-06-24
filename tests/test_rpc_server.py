import json, threading, urllib.error, urllib.request
from conftest import EXECUTOR
from daemon.events import EventQueue
from daemon.rpc_server import make_server


class FakeManager:
    def __init__(self):
        self._events = EventQueue(); self.started = None; self.responded = []; self.stopped = []
    def start(self, executor, task): self.started = (executor, task); return "s1"
    def respond(self, session_id, event_id, answer):
        self.responded.append((session_id, event_id, answer)); return event_id == "ok"
    def status(self, session_id=None): return {"sessions": {}} if session_id is None else {"state": "working"}
    def stop(self, session_id): self.stopped.append(session_id); return True


def _req(method, url, token="t", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={"X-Nelix-Token": token})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_rpc_session_scoped_roundtrip():
    m = FakeManager()
    srv = make_server(m, token="t", port=8766)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:8766"
    try:
        st, b = _req("POST", base + "/start", body={"executor": EXECUTOR, "task": "hi"})
        assert st == 200 and b["session_id"] == "s1" and m.started == (EXECUTOR, "hi")
        m._events.publish("s1", EXECUTOR, "waiting_for_user", "y/n?", "waiting_for_user")
        _, wb = _req("GET", base + "/wait?after_seq=0")
        eid = wb["event"]["event_id"]; assert wb["event"]["session_id"] == "s1"
        st, _ = _req("POST", base + "/respond",
                     body={"session_id": "s1", "event_id": "ok", "answer": "yes"})
        assert st == 200 and m.responded == [("s1", "ok", "yes")]
        st, _ = _req("POST", base + "/respond",
                     body={"session_id": "s1", "event_id": "stale", "answer": "yes"})
        assert st == 409
        st, _ = _req("POST", base + "/stop", body={"session_id": "s1"})
        assert st == 200 and m.stopped == ["s1"]
    finally:
        srv.shutdown()


def test_rpc_requires_token():
    srv = make_server(FakeManager(), token="t", port=8767)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = urllib.request.Request("http://127.0.0.1:8767/status", method="GET")
        try:
            urllib.request.urlopen(r, timeout=5); assert False, "expected 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        srv.shutdown()


class FakeManagerRaisesValueError:
    def __init__(self):
        self._events = EventQueue()
    def start(self, executor, task):
        raise ValueError(f"launcher 'auto' is not implemented (post-MVP); use 'local'")
    def respond(self, *a): return False
    def status(self, session_id=None): return {}
    def stop(self, session_id): return False


def test_rpc_start_value_error_returns_409():
    m = FakeManagerRaisesValueError()
    srv = make_server(m, token="t", port=8768)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", "http://127.0.0.1:8768/start",
                     body={"executor": "bad", "task": "hi"})
        assert st == 409, f"expected 409, got {st}"
        assert "error" in b
    finally:
        srv.shutdown()


class _FakeDialog:
    def turn_count(self): return 3
    def turn_text(self, turn, offset=0, limit=None):
        return {"turn_index": turn, "text": f"turn{turn}@{offset}", "total_len": 5,
                "truncated": False, "unavailable": False}


class _FakeSession:
    dialog = _FakeDialog()


class FakeManagerWithDialog:
    def __init__(self): self._events = EventQueue()
    def status(self, sid=None):
        return {"session_id": "s1", "executor": EXECUTOR, "state": "idle_prompt",
                "turn_count": 3, "decision": {"kind": "waiting_for_user", "turn_index": 2,
                "text": "Proceed?", "hint": "needs_permission"}}
    def get(self, sid): return _FakeSession() if sid == "s1" else None


def test_status_includes_decision():
    m = FakeManagerWithDialog()
    srv = make_server(m, token="t", port=8770)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("GET", "http://127.0.0.1:8770/status?session_id=s1")
        assert st == 200 and b["decision"]["kind"] == "waiting_for_user"
        assert b["decision"]["hint"] == "needs_permission"
    finally:
        srv.shutdown()


def test_dialog_paginates_turn_and_defaults_to_latest():
    m = FakeManagerWithDialog()
    srv = make_server(m, token="t", port=8771)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("GET", "http://127.0.0.1:8771/dialog?session_id=s1&turn=1&offset=2")
        assert st == 200 and b["turn_index"] == 1 and b["text"] == "turn1@2"
        _, b = _req("GET", "http://127.0.0.1:8771/dialog?session_id=s1")
        assert b["turn_index"] == 2                      # default -> latest (turn_count-1)
        st, _ = _req("GET", "http://127.0.0.1:8771/dialog?session_id=nope")
        assert st == 404
    finally:
        srv.shutdown()


def test_rpc_start_missing_field_returns_400():
    m = FakeManager()
    srv = make_server(m, token="t", port=8769)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # body missing "task" key
        st, b = _req("POST", "http://127.0.0.1:8769/start",
                     body={"executor": EXECUTOR})
        assert st == 400, f"expected 400, got {st}"
        assert "missing field" in b.get("error", "")
    finally:
        srv.shutdown()
