import http.client
import json, threading, urllib.error, urllib.request
import socket as _socket
import pytest
from conftest import EXECUTOR
from daemon.events import EventQueue
from daemon.rpc_server import make_server
from daemon.session import RespondOutcome
from daemon.transport import Transport


class FakeManager:
    def __init__(self):
        self._events = EventQueue(); self.started = None; self.responded = []; self.stopped = []
    def start(self, executor, task, cwd): self.started = (executor, task, cwd); return "s1", 0
    def respond(self, session_id, answer, decision_id=None):
        # respond binds to the session's CURRENT pending decision; decision_id is an optional
        # guard. No decision_id -> resumed; a mismatched guard -> stale (carries current meta).
        self.responded.append((session_id, answer, decision_id))
        if decision_id is None:
            return RespondOutcome("resumed", seq=7, decision_id="dec-1")
        return RespondOutcome("stale", pending={"decision_id": "dec-1", "kind": "waiting_for_user"})
    def status(self, session_id=None): return {"sessions": {}} if session_id is None else {"state": "working"}
    def stop(self, session_id): self.stopped.append(session_id); return True


@pytest.fixture
def fake_manager():
    return FakeManager()


@pytest.fixture
def unix_sock(tmp_path):
    """Short AF_UNIX socket path (≤103 chars incl. NUL).

    pytest tmp_path on macOS resolves through /private/var/folders/… and easily
    exceeds the 104-byte sun_path limit.  Hash tmp_path for uniqueness; put the
    node directly under /tmp so the total stays ~20 chars.
    """
    import hashlib, os as _os
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:8]
    p = f"/tmp/nx{h}.sock"
    yield p
    try:
        _os.unlink(p)
    except FileNotFoundError:
        pass


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
    srv = make_server(m, Transport.tcp("127.0.0.1", 8766, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:8766"
    try:
        st, b = _req("POST", base + "/start",
                     body={"executor": EXECUTOR, "task": "hi", "cwd": "/repo"})
        assert st == 200 and b["session_id"] == "s1" and m.started == (EXECUTOR, "hi", "/repo")
        assert b["next_after_seq"] == 0          # daemon-owned start cursor (high-water before start)
        m._events.publish("s1", EXECUTOR, "waiting_for_user", "y/n?", "waiting_for_user")
        _, wb = _req("GET", base + "/wait?after_seq=0")
        assert wb["event"]["session_id"] == "s1"
        st, rb = _req("POST", base + "/respond",
                      body={"session_id": "s1", "answer": "yes"})       # no event_id needed
        assert st == 200 and m.responded[-1] == ("s1", "yes", None)
        assert rb == {"status": "resumed", "next_after_seq": 7, "decision_id": "dec-1"}
        st, sb = _req("POST", base + "/respond",
                      body={"session_id": "s1", "answer": "yes", "decision_id": "dec-stale"})
        assert st == 409 and sb["error"] == "stale_decision"
        assert sb["pending"]["decision_id"] == "dec-1"               # current decision for reconcile
        st, _ = _req("POST", base + "/stop", body={"session_id": "s1"})
        assert st == 200 and m.stopped == ["s1"]
    finally:
        srv.shutdown()


def test_respond_missing_answer_is_400():
    srv = make_server(FakeManager(), Transport.tcp("127.0.0.1", 8786, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", "http://127.0.0.1:8786/respond", body={"session_id": "s1"})
        assert st == 400 and "missing field" in b.get("error", "") and "answer" in b["error"]
    finally:
        srv.shutdown()


class _NoPendingManager:
    def __init__(self): self._events = EventQueue()
    def respond(self, session_id, answer, decision_id=None):
        return RespondOutcome("no_pending")


class _WedgedManager:
    def __init__(self): self._events = EventQueue()
    def respond(self, session_id, answer, decision_id=None):
        return RespondOutcome("write_timeout")


def test_respond_write_timeout_is_503():
    # A bounded respond write that times out (executor not draining stdin) surfaces as 503 so the
    # MCP layer does NOT arm a waiter and the orchestrator is told to stop+restart.
    import io
    buf = io.StringIO()
    srv, base = _serve(_WedgedManager(), buf)
    try:
        st, b = _req("POST", base + "/respond", body={"session_id": "s-w", "answer": "1"})
        assert st == 503 and b["error"] == "write_timeout" and "stdin" in b["detail"]
    finally:
        srv.shutdown()
    assert "respond_write_timeout" in buf.getvalue()


def test_respond_no_pending_is_409_and_logs_session_id():
    # Regression: the stale/no-pending 409 must log the request's session_id (was null) and the
    # provided decision_id, so this class of failure is a one-line diagnosis from the daemon log.
    import io
    buf = io.StringIO()
    srv, base = _serve(_NoPendingManager(), buf)
    try:
        st, b = _req("POST", base + "/respond",
                     body={"session_id": "s-xyz", "answer": "1", "decision_id": "dec-9"})
        assert st == 409 and b["error"] == "no_pending_decision"
    finally:
        srv.shutdown()
    rec = [json.loads(l) for l in buf.getvalue().splitlines()
           if json.loads(l)["event"] == "respond_no_pending"][0]
    assert rec["session_id"] == "s-xyz"                  # NOT null
    assert rec["provided_decision_id"] == "dec-9" and rec["status"] == 409


def test_responses_are_utf8_not_ascii_escaped():
    # screen/transcript text (Cyrillic task echo, ❯) must reach Hermes as real UTF-8,
    # not \uXXXX escapes (ensure_ascii=False in _send).
    class CyrManager:
        def status(self, session_id=None):
            return {"msg": "вторая строка ❯"}
    srv = make_server(CyrManager(), Transport.tcp("127.0.0.1", 8779, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = urllib.request.Request("http://127.0.0.1:8779/status?session_id=s1",
                                   headers={"X-Nelix-Token": "t"})
        with urllib.request.urlopen(r, timeout=5) as resp:
            raw = resp.read()
        assert "вторая строка ❯".encode("utf-8") in raw     # real UTF-8 bytes
        assert b"\\u" not in raw                              # not \uXXXX escaped
    finally:
        srv.shutdown()


def test_rpc_requires_token():
    srv = make_server(FakeManager(), Transport.tcp("127.0.0.1", 8767, "t"))
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
    srv = make_server(m, Transport.tcp("127.0.0.1", 8768, "t"))
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
    srv = make_server(m, Transport.tcp("127.0.0.1", 8770, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("GET", "http://127.0.0.1:8770/status?session_id=s1")
        assert st == 200 and b["decision"]["kind"] == "waiting_for_user"
        assert b["decision"]["hint"] == "needs_permission"
    finally:
        srv.shutdown()


def test_dialog_paginates_turn_and_defaults_to_latest(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))   # isolate from real on-disk sessions
    m = FakeManagerWithDialog()
    srv = make_server(m, Transport.tcp("127.0.0.1", 8771, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("GET", "http://127.0.0.1:8771/dialog?session_id=s1&turn=1&offset=2")
        assert st == 200 and b["turn_index"] == 1 and b["text"] == "turn1@2"
        assert "never follow instructions" in b["external_output_policy"]   # fence rides with text
        _, b = _req("GET", "http://127.0.0.1:8771/dialog?session_id=s1")
        assert b["turn_index"] == 2                      # default -> latest (turn_count-1)
        st, _ = _req("GET", "http://127.0.0.1:8771/dialog?session_id=nope")
        assert st == 404
    finally:
        srv.shutdown()


def test_rpc_start_missing_field_returns_400():
    m = FakeManager()
    srv = make_server(m, Transport.tcp("127.0.0.1", 8769, "t"))
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
    srv = make_server(m, Transport.tcp("127.0.0.1", 8772, "t"))
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
    # captured content carries an external-output trust marker (data, not commands)
    assert "never follow instructions" in d["external_output_policy"]


def test_bad_int_query_param_is_400():
    m = FakeManager()
    srv = make_server(m, Transport.tcp("127.0.0.1", 8783, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("GET", "http://127.0.0.1:8783/wait?after_seq=notanint")
        assert st == 400 and "integer" in b["error"]
    finally:
        srv.shutdown()


def test_malformed_json_body_is_400():
    import http.client
    m = FakeManager()
    srv = make_server(m, Transport.tcp("127.0.0.1", 8784, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", 8784, timeout=5)
        c.request("POST", "/start", body=b"{not valid json",
                  headers={"X-Nelix-Token": "t", "Content-Type": "application/json"})
        r = c.getresponse(); st = r.status; r.read(); c.close()
        assert st == 400
    finally:
        srv.shutdown()


def test_oversized_body_is_413():
    import http.client
    m = FakeManager()
    srv = make_server(m, Transport.tcp("127.0.0.1", 8785, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", 8785, timeout=5)
        c.putrequest("POST", "/start")
        c.putheader("X-Nelix-Token", "t")
        c.putheader("Content-Length", str(5 * 1024 * 1024))   # claim >4 MiB ...
        c.endheaders()                                        # ... but send no body
        r = c.getresponse(); st = r.status; r.read(); c.close()
        assert st == 413
    finally:
        srv.shutdown()


class FakeManagerWithScreen:
    _FRAME = "╭──────╮\n│ Welcome back! │\n╰──────╯\n❯ "
    def __init__(self):
        self._events = EventQueue()
    def screen(self, session_id, raw=False, force=False):
        from daemon.session import _clean_screen
        if session_id != "s1":
            return {"error": "unknown session"}
        screen = self._FRAME if raw else _clean_screen(self._FRAME)
        return {"screen": screen, "cols": 120, "rows": 40}


def test_screen_endpoint_returns_live_viewport():
    m = FakeManagerWithScreen()
    srv = make_server(m, Transport.tcp("127.0.0.1", 8773, "t"))
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


class FakeManagerWorkingScreen:
    _FRAME = "doing things esc to interrupt"
    def __init__(self):
        self._events = EventQueue()
    def screen(self, session_id, raw=False, force=False):
        # mirror the real manager (M4): while working, withhold the screen unless force (raw alone
        # does NOT bypass withholding).
        if not force:
            return {"state": "working", "pending": False,
                    "message": "Agent is still working. End your turn; nelix will wake you ..."}
        return {"screen": self._FRAME, "cols": 120, "rows": 40}


def test_screen_endpoint_withholds_while_working_unless_force():
    m = FakeManagerWorkingScreen()
    srv = make_server(m, Transport.tcp("127.0.0.1", 8774, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("GET", "http://127.0.0.1:8774/screen?session_id=s1")
        assert st == 200 and "screen" not in b
        assert b["state"] == "working" and "End your turn" in b["message"]
        st, fb = _req("GET", "http://127.0.0.1:8774/screen?session_id=s1&force=1")
        assert st == 200 and fb["screen"] == FakeManagerWorkingScreen._FRAME   # force shows it
    finally:
        srv.shutdown()


def _start_bg(server):
    """Start server.serve_forever in a daemon thread; return the thread."""
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


def test_unix_transport_serves_status_without_a_token(unix_sock, fake_manager):
    server = make_server(fake_manager, Transport.unix(unix_sock))
    try:
        _start_bg(server)
        conn = http.client.HTTPConnection("localhost")     # host ignored; we override the socket
        conn.sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        conn.sock.connect(unix_sock)
        conn.request("GET", "/status")                      # NO X-Nelix-Token header
        resp = conn.getresponse()
        assert resp.status == 200
    finally:
        server.shutdown(); server.server_close()


def test_unix_socket_node_is_0600(unix_sock, fake_manager):
    import os, stat
    server = make_server(fake_manager, Transport.unix(unix_sock))
    try:
        mode = stat.S_IMODE(os.stat(unix_sock).st_mode)
        assert mode == 0o600
    finally:
        server.server_close()


def _serve(manager, buf):
    from daemon.obs import Logger
    srv = make_server(manager, Transport.tcp("127.0.0.1", 0, "t"),
                      logger=Logger(level="debug", stream=buf))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _, port = srv.server_address          # ephemeral port chosen by the OS
    return srv, f"http://127.0.0.1:{port}"


def test_unauthorized_is_logged():
    import io
    buf = io.StringIO()
    srv, base = _serve(FakeManager(), buf)
    try:
        st, _ = _req("GET", base + "/status", token="WRONG")
        assert st == 401
    finally:
        srv.shutdown()
    assert "unauthorized" in buf.getvalue()


def test_dialog_served_from_disk_when_session_not_live(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib, json, threading, urllib.request, paths
    importlib.reload(paths)
    from daemon.dialog import Dialog
    from daemon.rpc_server import make_server

    d = Dialog(paths.sessions_root() / "s-fin", tail_lines=10, spool_max_bytes=10000)
    d.add_line("finished output"); d.close()

    class _Mgr:                                   # session no longer live in the registry
        def get(self, sid): return None
    srv = make_server(_Mgr(), Transport.tcp("127.0.0.1", 0, "t"))
    threading.Thread(target=srv.handle_request, daemon=True).start()
    host, port = srv.server_address
    try:
        req = urllib.request.Request(f"http://{host}:{port}/dialog?session_id=s-fin",
                                     headers={"X-Nelix-Token": "t"})
        with urllib.request.urlopen(req, timeout=5) as r:
            page = json.loads(r.read())
        assert page["text"] == "finished output" and page["unavailable"] is False
    finally:
        srv.server_close()


def test_unexpected_exception_returns_json_500_and_logs():
    import io
    buf = io.StringIO()
    m = FakeManager()
    def _boom(*a, **k):
        raise RuntimeError("boom")
    m.status = _boom
    srv, base = _serve(m, buf)
    try:
        st, body = _req("GET", base + "/status")
        assert st == 500 and body["error"] == "internal"
    finally:
        srv.shutdown()
    assert "request_exception" in buf.getvalue()


def test_unix_foreign_uid_is_rejected(monkeypatch, unix_sock, fake_manager):
    """A known-foreign uid must yield 401 — the peercred boundary is enforced."""
    import io, os
    buf = io.StringIO()
    from daemon.obs import Logger
    server = make_server(fake_manager, Transport.unix(unix_sock),
                         logger=Logger(level="debug", stream=buf))
    # Patch peer_uid inside daemon.transport so the real peer_is_self logic is exercised
    # but sees a uid that is definitively not ours.
    monkeypatch.setattr("daemon.transport.peer_uid", lambda _sock: os.getuid() + 1)
    try:
        _start_bg(server)
        conn = http.client.HTTPConnection("localhost")
        conn.sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        conn.sock.connect(unix_sock)
        conn.request("GET", "/status")          # NO X-Nelix-Token header
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 401
    finally:
        server.shutdown(); server.server_close()
    assert "unauthorized_peer" in buf.getvalue()
