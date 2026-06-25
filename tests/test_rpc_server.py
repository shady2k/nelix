import json, threading, urllib.error, urllib.request
from conftest import EXECUTOR
from daemon.events import EventQueue
from daemon.rpc_server import make_server


class FakeManager:
    def __init__(self):
        self._events = EventQueue(); self.started = None; self.responded = []; self.stopped = []
    def start(self, executor, task, cwd): self.started = (executor, task, cwd); return "s1", 0
    def respond(self, session_id, event_id, answer):
        # daemon owns the cursor: returns the answered seq, or None on stale/unknown
        self.responded.append((session_id, event_id, answer))
        return 7 if event_id == "ok" else None
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
        st, b = _req("POST", base + "/start",
                     body={"executor": EXECUTOR, "task": "hi", "cwd": "/repo"})
        assert st == 200 and b["session_id"] == "s1" and m.started == (EXECUTOR, "hi", "/repo")
        assert b["next_after_seq"] == 0          # daemon-owned start cursor (high-water before start)
        m._events.publish("s1", EXECUTOR, "waiting_for_user", "y/n?", "waiting_for_user")
        _, wb = _req("GET", base + "/wait?after_seq=0")
        eid = wb["event"]["event_id"]; assert wb["event"]["session_id"] == "s1"
        st, rb = _req("POST", base + "/respond",
                      body={"session_id": "s1", "event_id": "ok", "answer": "yes"})
        assert st == 200 and m.responded == [("s1", "ok", "yes")]
        assert rb == {"status": "resumed", "next_after_seq": 7}    # daemon-owned respond cursor
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
    def start(self, executor, task, cwd):
        raise ValueError(f"launcher 'auto' is not implemented (post-MVP); use 'local'")
    def respond(self, *a): return None
    def status(self, session_id=None): return {}
    def stop(self, session_id): return False


def test_rpc_start_value_error_returns_409():
    m = FakeManagerRaisesValueError()
    srv = make_server(m, token="t", port=8768)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", "http://127.0.0.1:8768/start",
                     body={"executor": "bad", "task": "hi", "cwd": "/repo"})
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


def test_rpc_start_missing_cwd_returns_400():
    m = FakeManager()
    srv = make_server(m, token="t", port=8772)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # cwd is required: a start without a working dir must be rejected, not defaulted.
        st, b = _req("POST", "http://127.0.0.1:8772/start",
                     body={"executor": EXECUTOR, "task": "hi"})
        assert st == 400, f"expected 400, got {st}"
        assert "missing field" in b.get("error", "") and "cwd" in b.get("error", "")
    finally:
        srv.shutdown()


def test_evt_dict_includes_new_fields():
    from daemon.rpc_server import _evt_dict
    from daemon.events import EventQueue
    q = EventQueue()
    e = q.publish("s-1", "agent", "blocked", "trust?", "startup_interstitial",
                  hint="task_not_delivered", task_delivery="pending",
                  requires_response=True, screen_excerpt="❯ 1. Yes")
    d = _evt_dict(e)
    for k in ("hint", "hung", "task_delivery", "requires_response", "screen_excerpt"):
        assert k in d
    assert d["task_delivery"] == "pending" and d["requires_response"] is True


class FakeManagerWithScreen:
    _FRAME = "╭──────╮\n│ Welcome back! │\n╰──────╯\n❯ "
    def __init__(self):
        self._events = EventQueue()
    def screen(self, session_id, raw=False):
        from daemon.session import _clean_screen
        if session_id != "s1":
            return {"error": "unknown session"}
        screen = self._FRAME if raw else _clean_screen(self._FRAME)
        return {"screen": screen, "cols": 120, "rows": 40}


def test_screen_endpoint_returns_live_viewport():
    m = FakeManagerWithScreen()
    srv = make_server(m, token="t", port=8773)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("GET", "http://127.0.0.1:8773/screen?session_id=s1")
        assert st == 200 and "screen" in b and isinstance(b["screen"], str)
        assert b["cols"] == 120 and b["rows"] == 40
        assert "│" not in b["screen"] and "Welcome back!" in b["screen"]   # cleaned by default
        st, rb = _req("GET", "http://127.0.0.1:8773/screen?session_id=s1&raw=1")
        assert st == 200 and "│" in rb["screen"]                            # raw is uncleaned
    finally:
        srv.shutdown()
