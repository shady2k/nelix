import http.client
import json, threading, urllib.error, urllib.request
import socket as _socket
import pytest
from tests.conftest import EXECUTOR, OWNER, own, serve
from daemon.events import EventQueue
from daemon.manager import StartOutcome, StopOutcome
from daemon.rpc_server import make_server
from daemon.session import RespondOutcome
from daemon.transport import Transport


class FakeManager:
    def __init__(self):
        self._events = EventQueue(); self.started = None; self.responded = []; self.stopped = []
        self.respond_status = "resumed"; self.started_model = "__unset__"
    def start(self, executor, task, cwd, *, owner_id, model=None, session_id=None):
        self.started = (executor, task, cwd); self.started_model = model
        return StartOutcome(session_id="s-00000001", base_seq=0,
                            snapshot={"session_id": "s-00000001", "control_state": "busy",
                                      "task_delivery": "pending", "pending": False})
    def respond(self, session_id, answer, *, owner_id, decision_id=None):
        self.responded.append((session_id, answer, decision_id)); s = self.respond_status
        if s == "resumed":
            return RespondOutcome("resumed", seq=7, decision_id="dec-1", answered_decision_id="dec-1",
                                  snapshot={"session_id": session_id, "control_state": "busy", "pending": False})
        if s == "write_timeout":
            return RespondOutcome("write_timeout", answered_decision_id="dec-1",
                                  snapshot={"session_id": session_id, "control_state": "busy", "pending": False})
        if s == "unknown_session":
            return RespondOutcome("unknown_session")
        if s == "missing_decision_id":
            return RespondOutcome("missing_decision_id",
                                  pending={"decision_id": "dec-1", "kind": "waiting_for_user", "text": "y/n?"})
        return RespondOutcome("stale", pending={"decision_id": "dec-1", "kind": "waiting_for_user",
                                                "text": "y/n?"})
    def status(self, session_id=None, *, owner_id, include_progress=False):
        return {"sessions": {}} if session_id is None else {"state": "working"}
    def stop(self, session_id, *, owner_id):
        self.stopped.append(session_id)
        return StopOutcome("stopped", snapshot={"session_id": session_id,
                                                "control_state": "terminal",
                                                "terminal_kind": "stopped", "pending": False})


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


def _req(method, url, token="t", body=None, headers=None):
    """`headers` merges over the transport token (the /hook and /message routes need to send their
    per-session secret). An empty response body decodes to {} rather than raising: /hook answers
    204 No Content on success, and a helper that cannot read a success is no helper."""
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"X-Nelix-Token": token, **(headers or {})}
    r = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_rpc_session_scoped_roundtrip():
    own("s-00000001")   # /wait only arms on a session the caller owns
    m = FakeManager()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", base + "/start",
                     body={"executor": EXECUTOR, "task": "hi", "cwd": "/repo", "owner_id": OWNER})
        assert st == 200 and b["operation"] == "start" and b["session_id"] == "s-00000001"
        assert b["next_after_seq"] == 0          # daemon-owned start cursor (high-water before start)
        assert m.started == (EXECUTOR, "hi", "/repo")
        m._events.publish("s-00000001", EXECUTOR, "waiting_for_user", "y/n?", "waiting_for_user")
        _, wb = _req("GET", base + f"/wait?owner_id={OWNER}&after_seq=0&session_id=s-00000001")
        assert wb["event"]["session_id"] == "s-00000001"
        st, rb = _req("POST", base + "/respond",
                      body={"session_id": "s-00000001", "answer": "yes", "decision_id": "dec-1", "owner_id": OWNER})  # names the decision
        assert st == 200 and m.responded[-1] == ("s-00000001", "yes", "dec-1")
        assert rb["operation"] == "respond" and rb["status"] == "resumed"
        assert rb["next_after_seq"] == 7 and rb["next_action"] == "end_turn"
        m.respond_status = "stale"
        st, sb = _req("POST", base + "/respond",
                      body={"session_id": "s-00000001", "answer": "yes", "decision_id": "dec-stale", "owner_id": OWNER})
        assert st == 409 and sb["status"] == "stale"
        assert sb["pending"]["decision_id"] == "dec-1"               # current decision for reconcile
        assert sb["next_action"] == "fix_call"
        st, _ = _req("POST", base + "/stop", body={"session_id": "s-00000001", "owner_id": OWNER})
        assert st == 200 and m.stopped == ["s-00000001"]
    finally:
        srv.shutdown()


def test_wait_returns_cursor_expired_when_cursor_fell_off_the_ring():
    # nelix-9a4.5 deliverable 3: a /wait armed at a cursor whose events were evicted (the ring
    # dropped them) must answer with an EXPLICIT cursor_expired resync marker, never a silent
    # event:null — otherwise a wake-driven caller stalls forever and never re-/status's.
    own("s-00000001")
    m = FakeManager()
    # small ring, no floor: a flood on ANOTHER session drops s-00000001's only event.
    m._events = EventQueue(max_history=5, owner_floor=0)
    own("s-flood01")
    m._events.publish("s-00000001", EXECUTOR, "done", "old doorbell", "done_candidate")
    for _ in range(20):
        m._events.publish("s-flood01", EXECUTOR, "working", "", "working")
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _, wb = _req("GET", base + f"/wait?owner_id={OWNER}&after_seq=0&session_id=s-00000001")
        assert wb["event"] is None
        assert wb["cursor_expired"] is True
    finally:
        srv.shutdown()


# ============================================================ 3c.3b: MULTI-SESSION /wait route
#
# The daemon /wait route accepts repeated session_id= params (the router's orchestration wait).
# Owner-gates EACH sid, waits only on the OWNED subset, and an all-foreign set 404s like the
# single un-armable wait — never a 200/null spin. Single-session /wait stays byte-for-byte
# backward compatible (the roundtrip test above already pins it).

def _wait_srv():
    own("s-0000000a"); own("s-0000000b")
    m = FakeManager()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return m, srv, base


def test_wait_multi_session_wakes_on_any_owned_member():
    m, srv, base = _wait_srv()
    try:
        m._events.publish("s-0000000b", EXECUTOR, "waiting_for_user", "y/n?", "waiting_for_user")
        _, wb = _req("GET", base + f"/wait?owner_id={OWNER}&after_seq=0"
                                   f"&session_id=s-0000000a&session_id=s-0000000b")
        assert wb["event"]["session_id"] == "s-0000000b"    # the member that published woke it
        assert wb["event"]["seq"] == 1
    finally:
        srv.shutdown()


def test_wait_multi_session_skips_a_non_member_and_returns_the_member():
    # A non-member event (LOWER seq) is out of scope and must be skipped; the member's event is
    # returned instead. Proves the route filters the SET (without blocking the full 25s window).
    m, srv, base = _wait_srv()
    try:
        own("s-0000000c")
        m._events.publish("s-0000000c", EXECUTOR, "working", "not mine", "working")   # seq 1, OUT
        m._events.publish("s-0000000b", EXECUTOR, "waiting_for_user", "mine", "waiting_for_user")  # 2
        _, wb = _req("GET", base + f"/wait?owner_id={OWNER}&after_seq=0"
                                   f"&session_id=s-0000000a&session_id=s-0000000b")
        assert wb["event"]["session_id"] == "s-0000000b"    # the member, not the outsider s-0000000c
    finally:
        srv.shutdown()


def test_wait_multi_session_skips_an_unowned_sid_and_waits_on_the_owned_one():
    # A foreign/unowned sid in the set is SKIPPED (never waited on — it would deliver another owner's
    # event here); the owned member is still waited on and wakes.
    m, srv, base = _wait_srv()
    try:
        own("s-000000ff", owner_id="harness-y")             # a DIFFERENT owner's session
        m._events.publish("s-0000000a", EXECUTOR, "waiting_for_user", "mine", "waiting_for_user")
        _, wb = _req("GET", base + f"/wait?owner_id={OWNER}&after_seq=0"
                                   f"&session_id=s-0000000a&session_id=s-000000ff")
        assert wb["event"]["session_id"] == "s-0000000a"
    finally:
        srv.shutdown()


def test_wait_multi_session_all_foreign_set_404s_never_a_null_spin():
    # An all-foreign set reduces to empty -> a wait that can NEVER wake. 404 like the single
    # un-armable wait, never a 200/null the caller would re-issue at ~3400 req/s.
    m, srv, base = _wait_srv()
    try:
        own("s-000000f1", owner_id="harness-y")
        own("s-000000f2", owner_id="harness-y")
        st, wb = _req("GET", base + f"/wait?owner_id={OWNER}&after_seq=0"
                                    f"&session_id=s-000000f1&session_id=s-000000f2")
        assert st == 404
        assert "hint" in wb
    finally:
        srv.shutdown()


def test_wait_multi_session_bad_shape_member_is_a_400_before_any_wait():
    m, srv, base = _wait_srv()
    try:
        st, wb = _req("GET", base + f"/wait?owner_id={OWNER}&after_seq=0"
                                    f"&session_id=s-0000000a&session_id=not-a-session")
        assert st == 400
        assert wb["error"]["code"] == "invalid_session_id"
    finally:
        srv.shutdown()


def test_wait_multi_session_cursor_expired_is_relayed_for_the_set():
    own("s-0000000a")
    m = FakeManager()
    m._events = EventQueue(max_history=5, owner_floor=0)
    own("s-flood01")
    m._events.publish("s-0000000a", EXECUTOR, "done", "old doorbell", "done_candidate")
    for _ in range(20):
        m._events.publish("s-flood01", EXECUTOR, "working", "", "working")   # evicts the member's event
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        own("s-0000000b")
        _, wb = _req("GET", base + f"/wait?owner_id={OWNER}&after_seq=0"
                                   f"&session_id=s-0000000a&session_id=s-0000000b")
        assert wb["event"] is None and wb["cursor_expired"] is True
    finally:
        srv.shutdown()


def test_respond_without_decision_id_returns_409_missing():
    # A respond whose outcome is missing_decision_id surfaces as HTTP 409 with the pending
    # decision meta (incl. its text) so the orchestrator retries with the id — NOT a guessed 200.
    m = FakeManager()
    m.respond_status = "missing_decision_id"
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", base + "/respond",
                     body={"session_id": "s-00000001", "answer": "yes", "owner_id": OWNER})       # no decision_id
        assert st == 409 and b["operation"] == "respond"
        assert b["status"] == "missing_decision_id" and b["error"] == "missing_decision_id"
        assert b["pending"]["decision_id"] == "dec-1" and b["pending"]["text"] == "y/n?"
        assert b["next_action"] == "fix_call"
        assert m.responded[-1] == ("s-00000001", "yes", None)
    finally:
        srv.shutdown()


def test_respond_missing_answer_is_400():
    srv, base = serve(FakeManager())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", base + "/respond", body={"session_id": "s-00000001", "owner_id": OWNER})
        assert st == 400 and "missing field" in b.get("error", "") and "answer" in b["error"]
    finally:
        srv.shutdown()


class _NoPendingManager:
    def __init__(self): self._events = EventQueue()
    def respond(self, session_id, answer, *, owner_id, decision_id=None):
        return RespondOutcome("no_pending")


class _WedgedManager:
    def __init__(self): self._events = EventQueue()
    def respond(self, session_id, answer, *, owner_id, decision_id=None):
        return RespondOutcome("write_timeout",
                              snapshot={"session_id": "s-0000dead", "control_state": "busy", "pending": False})


def test_respond_write_timeout_is_503():
    # A bounded respond write that times out (executor not draining stdin) surfaces as 503 so the
    # MCP layer does NOT arm a waiter and the orchestrator is told to stop+restart.
    import io
    buf = io.StringIO()
    srv, base = _serve(_WedgedManager(), buf)
    try:
        st, b = _req("POST", base + "/respond", body={"session_id": "s-0000dead", "answer": "1", "owner_id": OWNER})
        assert st == 503 and b["status"] == "write_timeout" and b["next_action"] == "recover"
    finally:
        srv.shutdown()
    assert "respond_write_timeout" in buf.getvalue()


class _UnconfirmedManager:
    def __init__(self): self._events = EventQueue()
    def respond(self, session_id, answer, *, owner_id, decision_id=None):
        return RespondOutcome("respond_failed", decision_id="dec-1", answered_decision_id="dec-1",
                              snapshot={"session_id": "s-0000beef", "control_state": "busy", "pending": False})


def test_respond_unconfirmed_submit_is_503_recover():
    # nelix-sud: the answer was typed but never LEFT the box (submit unconfirmed). It must surface as
    # 503 with next_action='recover' (not a false 200/end_turn) so the MCP layer does NOT arm a
    # waiter and the orchestrator recovers instead of going silent.
    import io
    buf = io.StringIO()
    srv, base = _serve(_UnconfirmedManager(), buf)
    try:
        st, b = _req("POST", base + "/respond", body={"session_id": "s-0000beef", "answer": "do the thing", "owner_id": OWNER})
        assert st == 503 and b["status"] == "respond_failed" and b["next_action"] == "recover"
        assert b["error"] == "submit_unconfirmed"
        assert b["snapshot"]["pending"] is False
        assert b["answered_decision_id"] == "dec-1"
    finally:
        srv.shutdown()
    assert "respond_unconfirmed" in buf.getvalue()


class _AtCapacityManager:
    def __init__(self): self._events = EventQueue()
    def respond(self, session_id, answer, *, owner_id, decision_id=None):
        # An idle follow-up that can't re-acquire an active slot (concurrency cap full): the manager
        # routes it through send_turn, which returns at_capacity — an honest backpressure signal.
        return RespondOutcome("at_capacity")


def test_respond_at_capacity_is_503_honest_backpressure():
    # IMPORTANT 1: an idle follow-up refused for capacity must surface HONESTLY (503 at_capacity with
    # a retry-shaped next_action), NOT be mislabeled no_pending (409) — the decision exists, the slot
    # doesn't. The orchestrator can refresh_status / retry once a slot frees.
    import io
    buf = io.StringIO()
    srv, base = _serve(_AtCapacityManager(), buf)
    try:
        st, b = _req("POST", base + "/respond", body={"session_id": "s-0000cafe", "answer": "continue", "owner_id": OWNER})
        assert st == 503
        assert b["operation"] == "respond" and b["status"] == "at_capacity"
        assert b["status"] != "no_pending" and b.get("error") != "no_pending_decision"
        assert b["next_action"] in ("refresh_status", "retry")
    finally:
        srv.shutdown()
    assert "respond_at_capacity" in buf.getvalue()


class _NotDeliveredManager:
    """Stands in for Manager.respond returning the async-question RespondOutcome("not_delivered",
    ...) — either sub-case (Task 4 in-Session closing/terminal: reason=None, snapshot present; or
    Task 6 manager-level terminal-survival: reason="executor_finished", no snapshot)."""
    def __init__(self, reason=None, snapshot=None):
        self._events = EventQueue()
        self._reason = reason
        self._snapshot = snapshot

    def respond(self, session_id, answer, *, owner_id, decision_id=None):
        return RespondOutcome("not_delivered", reason=self._reason, snapshot=self._snapshot)


def test_respond_not_delivered_executor_finished_is_200_refresh_status():
    # Task 6 manager-level terminal-survival path: the executor had ALREADY exited before the async
    # answer arrived. This must be a clean, defined outcome (not a 4xx caller error) so the MCP layer
    # relays it plainly and Hermes reconciles via refresh_status instead of retrying the call.
    import io
    buf = io.StringIO()
    srv, base = _serve(_NotDeliveredManager(reason="executor_finished"), buf)
    try:
        st, b = _req("POST", base + "/respond",
                     body={"session_id": "s-00000001", "answer": "use a", "decision_id": "q_1", "owner_id": OWNER})
        assert st == 200
        assert b["operation"] == "respond" and b["status"] == "not_delivered"
        assert b["reason"] == "executor_finished"
        assert b["next_action"] == "refresh_status"
    finally:
        srv.shutdown()


def test_respond_not_delivered_in_session_closing_carries_snapshot_no_reason():
    # Task 4 in-Session closing/terminal path predates the `reason` field (always None there) but
    # DOES carry a snapshot — both must reach the caller so Hermes can tell the two sub-cases apart.
    snap = {"session_id": "s-00000001", "control_state": "terminal", "terminal_kind": "crashed"}
    srv, base = _serve(_NotDeliveredManager(reason=None, snapshot=snap), __import__("io").StringIO())
    try:
        st, b = _req("POST", base + "/respond",
                     body={"session_id": "s-00000001", "answer": "use a", "decision_id": "q_1", "owner_id": OWNER})
        assert st == 200 and b["status"] == "not_delivered"
        assert b["reason"] is None
        assert b["snapshot"]["terminal_kind"] == "crashed"
        assert b["next_action"] == "refresh_status"
    finally:
        srv.shutdown()


class _QueuedManager:
    """Manager.respond returning RespondOutcome("queued", snapshot=...) — the COMMON async case: the
    executor asked a non-blocking question, kept working, and Hermes's answer landed while it was
    still busy, so it was correlated + enqueued (monitor delivers at the next idle)."""
    def __init__(self):
        self._events = EventQueue()

    def respond(self, session_id, answer, *, owner_id, decision_id=None):
        return RespondOutcome("queued",
                              snapshot={"session_id": session_id, "control_state": "busy",
                                        "pending": False})


def test_respond_queued_async_answer_is_200_not_false_no_pending():
    # Regression: a busy-queued async answer must be a clean 200 status:"queued" (accepted, will be
    # delivered at the next idle), NOT the no_pending catch-all's 409/fix_call — that would misreport
    # the COMMON async path (executor still working) as a malformed call.
    srv, base = _serve(_QueuedManager(), __import__("io").StringIO())
    try:
        st, b = _req("POST", base + "/respond",
                     body={"session_id": "s-00000001", "answer": "use a", "decision_id": "q_1", "owner_id": OWNER})
        assert st == 200
        assert b["operation"] == "respond" and b["status"] == "queued"
        assert b["status"] != "no_pending" and b.get("error") != "no_pending_decision"
        assert b["next_action"] == "refresh_status"
        assert b["snapshot"]["control_state"] == "busy"
    finally:
        srv.shutdown()


def test_start_envelope_shape():
    m = FakeManager()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", base + "/start",
                     body={"executor": EXECUTOR, "task": "hi", "cwd": "/repo", "owner_id": OWNER})
        assert st == 200 and b["operation"] == "start" and b["status"] == "started"
        assert b["session_id"] == "s-00000001" and b["next_after_seq"] == 0
        assert b["snapshot"]["control_state"] == "busy" and b["next_action"] == "end_turn"
    finally:
        srv.shutdown()


def test_respond_write_timeout_is_503_recover():
    m = FakeManager(); m.respond_status = "write_timeout"
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", base + "/respond", body={"session_id": "s-00000001", "answer": "x", "owner_id": OWNER})
        assert st == 503 and b["status"] == "write_timeout" and b["next_action"] == "recover"
        assert b["snapshot"]["pending"] is False
    finally:
        srv.shutdown()


def test_respond_unknown_session_is_404_refresh_status():
    m = FakeManager(); m.respond_status = "unknown_session"
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", base + "/respond", body={"session_id": "s-00000001", "answer": "x", "owner_id": OWNER})
        assert st == 404 and b["operation"] == "respond" and b["status"] == "unknown_session"
        assert b["next_action"] == "refresh_status" and b["session_id"] == "s-00000001"
    finally:
        srv.shutdown()


def test_stop_confirmed_terminal_is_report():
    m = FakeManager()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", base + "/stop", body={"session_id": "s-00000001", "owner_id": OWNER})
        assert st == 200 and b["operation"] == "stop" and b["status"] == "stopped"
        assert b["next_action"] == "report" and b["snapshot"]["terminal_kind"] == "stopped"
    finally:
        srv.shutdown()


class _StopRequestedManager:
    """Manager that returns stop_requested from stop() (teardown not confirmed within join)."""
    def __init__(self):
        self._events = EventQueue()
    def stop(self, session_id, *, owner_id):
        return StopOutcome("stop_requested",
                           snapshot={"session_id": session_id,
                                     "control_state": "stopping", "pending": False})


def test_rpc_stop_requested_is_refresh_status():
    """/stop returning stop_requested must yield HTTP 200 with next_action='refresh_status'."""
    srv, base = _serve(_StopRequestedManager(), __import__("io").StringIO())
    try:
        st, b = _req("POST", base + "/stop", body={"session_id": "s-00000001", "owner_id": OWNER})
        assert st == 200 and b["operation"] == "stop"
        assert b["status"] == "stop_requested"
        assert b["next_action"] == "refresh_status"
        assert b["snapshot"]["control_state"] == "stopping"
    finally:
        srv.shutdown()


def test_respond_no_pending_is_409_and_logs_session_id():
    # Regression: the stale/no-pending 409 must log the request's session_id (was null) and the
    # provided decision_id, so this class of failure is a one-line diagnosis from the daemon log.
    import io
    buf = io.StringIO()
    srv, base = _serve(_NoPendingManager(), buf)
    try:
        st, b = _req("POST", base + "/respond",
                     body={"session_id": "s-0000abcd", "answer": "1", "decision_id": "dec-9", "owner_id": OWNER})
        assert st == 409 and b["error"] == "no_pending_decision"
    finally:
        srv.shutdown()
    rec = [json.loads(l) for l in buf.getvalue().splitlines()
           if json.loads(l)["event"] == "respond_no_pending"][0]
    assert rec["session_id"] == "s-0000abcd"                  # NOT null
    assert rec["provided_decision_id"] == "dec-9" and rec["status"] == 409


def test_responses_are_utf8_not_ascii_escaped():
    # screen/transcript text (Cyrillic task echo, ❯) must reach Hermes as real UTF-8,
    # not \uXXXX escapes (ensure_ascii=False in _send).
    class CyrManager:
        def status(self, session_id=None, *, owner_id, include_progress=False):
            return {"msg": "вторая строка ❯"}
    srv, base = serve(CyrManager())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = urllib.request.Request(base + f"/status?owner_id={OWNER}&session_id=s-00000001",
                                   headers={"X-Nelix-Token": "t"})
        with urllib.request.urlopen(r, timeout=5) as resp:
            raw = resp.read()
        assert "вторая строка ❯".encode("utf-8") in raw     # real UTF-8 bytes
        assert b"\\u" not in raw                              # not \uXXXX escaped
    finally:
        srv.shutdown()


def test_rpc_requires_token():
    srv, base = serve(FakeManager())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = urllib.request.Request(base + "/status", method="GET")
        try:
            urllib.request.urlopen(r, timeout=5); assert False, "expected 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        srv.shutdown()


class FakeManagerRaisesValueError:
    def __init__(self):
        self._events = EventQueue()
    def start(self, executor, task, cwd, *, owner_id, model=None, session_id=None):
        raise ValueError("launcher 'auto' is not implemented (post-MVP); use 'local'")
    def respond(self, *a): return None
    def status(self, session_id=None, *, owner_id, include_progress=False): return {}
    def stop(self, session_id, *, owner_id): return False


def test_rpc_start_threads_model_to_manager():
    # nelix-9k0: /start reads optional body["model"] and passes it to manager.start(..., model=..., owner_id=OWNER).
    m = FakeManager()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", base + "/start",
                     body={"executor": EXECUTOR, "task": "hi", "cwd": "/repo", "model": "haiku", "owner_id": OWNER})
        assert st == 200
        assert m.started_model == "haiku"
    finally:
        srv.shutdown()


def test_rpc_start_without_model_passes_none():
    # No model in the body -> manager.start receives model=None (byte-identical to pre-feature).
    m = FakeManager()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", base + "/start",
                     body={"executor": EXECUTOR, "task": "hi", "cwd": "/repo", "owner_id": OWNER})
        assert st == 200
        assert m.started_model is None
    finally:
        srv.shutdown()


class FakeManagerRejectsModel:
    def __init__(self): self._events = EventQueue()
    def start(self, executor, task, cwd, *, owner_id, model=None, session_id=None):
        from daemon.manager import ModelRejected
        raise ModelRejected("driver does not support a model override")
    def respond(self, *a): return None
    def status(self, session_id=None, *, owner_id, include_progress=False): return {}
    def stop(self, session_id, *, owner_id): return False


def test_rpc_start_model_rejected_returns_400():
    # ModelRejected is a ValueError subclass; /start must catch it BEFORE the generic
    # (RuntimeError, ValueError)->409 branch and return 400 (client input error, not daemon-full).
    m = FakeManagerRejectsModel()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", base + "/start",
                     body={"executor": EXECUTOR, "task": "hi", "cwd": "/repo", "model": "bad", "owner_id": OWNER})
        assert st == 400, f"expected 400, got {st}"
        assert "error" in b
    finally:
        srv.shutdown()


class FakeManagerModelUnavailable:
    def __init__(self): self._events = EventQueue()
    def start(self, executor, task, cwd, *, owner_id, model=None, session_id=None):
        from daemon.manager import ModelUnavailable
        raise ModelUnavailable([{"id": "glm-5.2", "display_name": "GLM-5.2"}])
    def respond(self, *a): return None
    def status(self, session_id=None, *, owner_id, include_progress=False): return {}
    def stop(self, session_id, *, owner_id): return False


def test_start_model_unavailable_returns_400_with_list():
    # nelix-kwr: an explicitly-requested model not offered by the backend -> 400 + the discovered
    # available_models list, so the caller can pick a real one (not a daemon-full 409).
    m = FakeManagerModelUnavailable()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", base + "/start",
                     body={"executor": EXECUTOR, "task": "hi", "cwd": "/repo", "model": "bad", "owner_id": OWNER})
        assert st == 400, f"expected 400, got {st}"
        assert b["available_models"] == [{"id": "glm-5.2", "display_name": "GLM-5.2"}]
    finally:
        srv.shutdown()


def test_rpc_start_value_error_returns_409():
    m = FakeManagerRaisesValueError()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("POST", base + "/start",
                     body={"executor": "bad", "task": "hi", "cwd": "/repo", "owner_id": OWNER})
        assert st == 409, f"expected 409, got {st}"
        assert "error" in b
    finally:
        srv.shutdown()


class _FakeDialog:
    """Fake dialog exposing the flat-log page() API used by the /dialog endpoint."""
    available = True

    def page(self, offset=0, limit=None, snap=True):
        text = f"transcript@{offset}"
        return {"text": text, "start_offset": offset, "next_offset": offset + len(text),
                "speaker_at_start": "agent", "continued": False, "total_len": 100}


class _FakeSession:
    dialog = _FakeDialog()


class FakeManagerWithDialog:
    def __init__(self): self._events = EventQueue()
    def status(self, sid=None, *, owner_id, include_progress=False):
        return {"session_id": "s1", "executor": EXECUTOR, "state": "idle_prompt",
                "decision": {"kind": "waiting_for_user",
                             "text": "Proceed?", "hint": "needs_permission"}}
    def get(self, sid): return _FakeSession() if sid == "s-00000001" else None


def test_status_includes_decision():
    m = FakeManagerWithDialog()
    own("s-00000001")   # the durable owner record a real start would have written
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("GET", base + f"/status?owner_id={OWNER}&session_id=s-00000001")
        assert st == 200 and b["decision"]["kind"] == "waiting_for_user"
        assert b["decision"]["hint"] == "needs_permission"
    finally:
        srv.shutdown()


class _ModalManager:
    def __init__(self): self._events = EventQueue()
    def status(self, sid=None, *, owner_id, include_progress=False):
        return {"session_id": "s1", "state": "awaiting_user",
                "decision": {"kind": "waiting_for_user", "prompt_kind": "modal_choice",
                             "options": [{"id": "1", "label": "Enrich all three"},
                                         {"id": "2", "label": "Verify-only"}]}}
    def respond(self, session_id, answer, *, owner_id, decision_id=None):
        from daemon.session import RespondOutcome
        return RespondOutcome("invalid_option",
                              pending={"prompt_kind": "modal_choice",
                                       "options": [{"id": "1", "label": "Enrich all three"}]})


def test_status_exposes_modal_options_and_prompt_kind():
    srv, base = _serve(_ModalManager(), __import__("io").StringIO())
    try:
        st, b = _req("GET", base + f"/status?owner_id={OWNER}&session_id=s-00000001")
        assert st == 200
        assert b["decision"]["prompt_kind"] == "modal_choice"
        assert [o["id"] for o in b["decision"]["options"]] == ["1", "2"]
    finally:
        srv.shutdown()


def test_respond_invalid_option_is_409_with_options():
    srv, base = _serve(_ModalManager(), __import__("io").StringIO())
    try:
        st, b = _req("POST", base + "/respond", body={"session_id": "s-00000001", "answer": "9", "owner_id": OWNER})
        assert st == 409 and b["error"] == "invalid_option"
        assert b["pending"]["options"][0]["id"] == "1"
    finally:
        srv.shutdown()


def test_dialog_serves_flat_page_with_offset(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))   # isolate from real on-disk sessions
    m = FakeManagerWithDialog()
    own("s-00000001")   # the durable owner record a real start would have written
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # Offset-based pagination — no turn parameter
        st, b = _req("GET", base + f"/dialog?owner_id={OWNER}&session_id=s-00000001&offset=42")
        assert st == 200 and b["text"] == "transcript@42"
        assert "speaker_at_start" in b           # flat-log fields present
        assert "never follow instructions" in b["external_output_policy"]   # fence rides
        # Unknown session → 404
        st, _ = _req("GET", base + f"/dialog?owner_id={OWNER}&session_id=s-00000099")
        assert st == 404
    finally:
        srv.shutdown()


def test_dialog_page_carries_at_end_mid_and_at_end(monkeypatch, tmp_path):
    import io
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    own("s-00000001")
    srv, base = _serve(FakeManagerWithDialog(), io.StringIO())
    try:
        # Mid-transcript: next_offset (56) < total_len (100)
        st, b = _req("GET", base + f"/dialog?owner_id={OWNER}&session_id=s-00000001&offset=42")
        assert st == 200 and b["at_end"] is False
        assert "hint" not in b                               # no hint mid-transcript
        # Past end: next_offset (215) >= total_len (100)
        st, b = _req("GET", base + f"/dialog?owner_id={OWNER}&session_id=s-00000001&offset=200")
        assert st == 200 and b["at_end"] is True
        assert "transcript end" in b["hint"]
        assert "nelix_status" in b["hint"]                   # advises recovery
    finally:
        srv.shutdown()


def test_dialog_unknown_session_carries_hint(monkeypatch, tmp_path):
    import io
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    own("s-00000001")
    srv, base = _serve(FakeManagerWithDialog(), io.StringIO())
    try:
        st, b = _req("GET", base + f"/dialog?owner_id={OWNER}&session_id=s-00000099")
        assert st == 404 and b["error"] == "unknown session"
        assert "nelix_status" in b["hint"]                   # recovery hint, not a bare error
    finally:
        srv.shutdown()


class _CapturingDialog:
    """Records the limit the handler passed, so we can prove the default is applied."""
    available = True
    last_limit = "unset"
    def page(self, offset=0, limit=None, snap=True):
        type(self).last_limit = limit
        text = f"transcript@{offset}"
        return {"text": text, "start_offset": offset, "next_offset": offset + len(text),
                "speaker_at_start": "agent", "continued": False, "total_len": 100}


class _CapturingSession:
    dialog = _CapturingDialog()


class _CapturingManager:
    def __init__(self): self._events = EventQueue()
    def status(self, sid=None, *, owner_id, include_progress=False): return {"sessions": {}}
    def get(self, sid): return _CapturingSession() if sid == "s-00000001" else None


def test_dialog_omitted_limit_uses_daemon_default(monkeypatch, tmp_path):
    import io
    from daemon.config import DEFAULT_DIALOG_PAGE_CHARS
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    _CapturingDialog.last_limit = "unset"
    own("s-00000001")
    srv, base = _serve(_CapturingManager(), io.StringIO())
    try:
        # No limit in the query → handler must substitute DEFAULT_DIALOG_PAGE_CHARS.
        _req("GET", base + f"/dialog?owner_id={OWNER}&session_id=s-00000001&offset=0")
        assert _CapturingDialog.last_limit == DEFAULT_DIALOG_PAGE_CHARS
        # Explicit limit still honored.
        _req("GET", base + f"/dialog?owner_id={OWNER}&session_id=s-00000001&offset=0&limit=123")
        assert _CapturingDialog.last_limit == 123
    finally:
        srv.shutdown()


def test_dialog_at_end_on_exact_final_page_real_reader(monkeypatch, tmp_path):
    """at_end is True on the final NON-EMPTY page (next_offset == total_len), not only past-end,
    so a caller needs no extra empty read. Exercises the real Dialog -> transcript.jsonl ->
    DialogReader -> handler path (not _FakeDialog)."""
    import io
    from daemon.dialog import Dialog
    import paths
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    sess_dir = paths.sessions_root() / "s-00000001"
    d = Dialog(sess_dir, tail_lines=10, spool_max_bytes=4096)
    own("s-00000001")
    d.add_agent_line("hello world")          # flat text becomes "‹agent›\nhello world"
    d.close()                                # flush transcript.jsonl to disk
    srv, base = _serve(FakeManagerWithDialog(), io.StringIO())
    try:
        st, b = _req("GET", base + f"/dialog?owner_id={OWNER}&session_id=s-00000001&offset=0")
        assert st == 200
        assert b["text"]                                    # non-empty final page
        assert b["next_offset"] == b["total_len"]           # read consumed the whole transcript
        assert b["at_end"] is True                          # exact final page -> at_end, no extra read
    finally:
        srv.shutdown()


def test_rpc_start_missing_field_returns_400():
    m = FakeManager()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # body missing "task" key
        st, b = _req("POST", base + "/start",
                     body={"executor": EXECUTOR, "owner_id": OWNER})
        assert st == 400, f"expected 400, got {st}"
        assert "missing field" in b.get("error", "")
    finally:
        srv.shutdown()


def test_rpc_start_missing_cwd_returns_400():
    m = FakeManager()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # cwd is required: a start without a working dir must be rejected, not defaulted.
        st, b = _req("POST", base + "/start",
                     body={"executor": EXECUTOR, "task": "hi", "owner_id": OWNER})
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


def test_rpc_wait_requires_session_id():
    # A /wait with no session_id is rejected (400) BEFORE any wait — never a global wait, which
    # would deliver another session's event into this caller (the cross-session leak this prevents).
    m = FakeManager()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # An event on a DIFFERENT session: a global waiter would leak it into this reply.
        m._events.publish("s-other", EXECUTOR, "waiting_for_user", "y/n?", "waiting_for_user")
        st, b = _req("GET", base + f"/wait?owner_id={OWNER}&after_seq=0")
        assert st == 400 and b["error"] == "missing session_id"
    finally:
        srv.shutdown()


def test_bad_int_query_param_is_400():
    m = FakeManager()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("GET", base + f"/wait?owner_id={OWNER}&after_seq=notanint")
        assert st == 400 and "integer" in b["error"]
    finally:
        srv.shutdown()


def test_malformed_json_body_is_400():
    import http.client
    m = FakeManager()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address
    try:
        c = http.client.HTTPConnection(host, port, timeout=5)
        c.request("POST", "/start", body=b"{not valid json",
                  headers={"X-Nelix-Token": "t", "Content-Type": "application/json"})
        r = c.getresponse(); st = r.status; r.read(); c.close()
        assert st == 400
    finally:
        srv.shutdown()


def test_oversized_body_is_413():
    import http.client
    m = FakeManager()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address
    try:
        c = http.client.HTTPConnection(host, port, timeout=5)
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
    def screen(self, session_id, *, owner_id, raw=False, force=False):
        from daemon.session import _clean_screen
        if session_id != "s-00000001":
            return {"error": "unknown session"}
        screen = self._FRAME if raw else _clean_screen(self._FRAME)
        return {"screen": screen, "cols": 120, "rows": 40}


def test_screen_endpoint_returns_live_viewport():
    m = FakeManagerWithScreen()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("GET", base + f"/screen?owner_id={OWNER}&session_id=s-00000001")
        assert st == 200 and "screen" in b and isinstance(b["screen"], str)
        assert b["cols"] == 120 and b["rows"] == 40
        assert "│" not in b["screen"] and "Welcome back!" in b["screen"]   # cleaned by default
        st, rb = _req("GET", base + f"/screen?owner_id={OWNER}&session_id=s-00000001&raw=1")
        assert st == 200 and "│" in rb["screen"]                            # raw is uncleaned
    finally:
        srv.shutdown()


class FakeManagerWorkingScreen:
    _FRAME = "doing things esc to interrupt"
    def __init__(self):
        self._events = EventQueue()
    def screen(self, session_id, *, owner_id, raw=False, force=False):
        # mirror the real manager (M4): while working, withhold the screen unless force (raw alone
        # does NOT bypass withholding).
        if not force:
            return {"control_state": "busy", "pending": False,
                    "message": "Agent is still working. End your turn; nelix will wake you ..."}
        return {"screen": self._FRAME, "cols": 120, "rows": 40}


def test_screen_endpoint_withholds_while_working_unless_force():
    m = FakeManagerWorkingScreen()
    srv, base = serve(m)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, b = _req("GET", base + f"/screen?owner_id={OWNER}&session_id=s-00000001")
        assert st == 200 and "screen" not in b
        assert b["control_state"] == "busy" and "End your turn" in b["message"]
        st, fb = _req("GET", base + f"/screen?owner_id={OWNER}&session_id=s-00000001&force=1")
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
        conn.request("GET", f"/status?owner_id={OWNER}")                      # NO X-Nelix-Token header
        resp = conn.getresponse()
        assert resp.status == 200
    finally:
        server.shutdown(); server.server_close()


def test_status_stamps_rpc_protocol_version(fake_manager):
    """/status always carries the RPC protocol version (regardless of session_id) so a supervisor
    can distinguish our daemon from one left running on stale code after a plugin update."""
    import io
    from daemon.protocol import RPC_PROTOCOL_VERSION
    srv, base = _serve(fake_manager, io.StringIO())
    try:
        st, body = _req("GET", base + f"/status?owner_id={OWNER}")
        assert st == 200
        assert body["rpc_protocol"] == RPC_PROTOCOL_VERSION
        # session-scoped status carries it too
        _, body2 = _req("GET", base + f"/status?owner_id={OWNER}&session_id=s-00000001")
        assert body2["rpc_protocol"] == RPC_PROTOCOL_VERSION
    finally:
        srv.shutdown()


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
        st, _ = _req("GET", base + f"/status?owner_id={OWNER}", token="WRONG")
        assert st == 401
    finally:
        srv.shutdown()
    assert "unauthorized" in buf.getvalue()


def test_status_read_is_logged(tmp_path):
    # nelix-jwv gap 4: a status/screen/dialog read leaves a light `read` record (which tool, session,
    # seq) at debug, so "nelix_screen called twice in a turn" is visible without replaying the capture.
    import io
    buf = io.StringIO()
    srv, base = _serve(FakeManager(), buf)
    try:
        st, _ = _req("GET", base + f"/status?owner_id={OWNER}&session_id=s-00000001")
        assert st == 200
    finally:
        srv.shutdown()
    reads = [json.loads(l) for l in buf.getvalue().splitlines()
             if l.strip() and json.loads(l)["event"] == "read"]
    assert reads, "a GET /status must emit a read record"
    r = reads[-1]
    assert r["level"] == "debug" and r["tool"] == "status" and r["session_id"] == "s-00000001"
    assert "seq" in r


def test_dialog_served_from_disk_when_session_not_live(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    import importlib, json, threading, urllib.request, paths
    importlib.reload(paths)
    from daemon.dialog import Dialog
    from daemon.rpc_server import make_server

    d = Dialog(paths.sessions_root() / "s-0000face", tail_lines=10, spool_max_bytes=10000)
    own("s-0000face")
    d.add_agent_line("finished output"); d.close()

    class _Mgr:                                   # session no longer live in the registry
        def get(self, sid): return None
    srv = make_server(_Mgr(), Transport.tcp("127.0.0.1", 0, "t"))
    threading.Thread(target=srv.handle_request, daemon=True).start()
    host, port = srv.server_address
    try:
        req = urllib.request.Request(f"http://{host}:{port}/dialog?owner_id={OWNER}&session_id=s-0000face",
                                     headers={"X-Nelix-Token": "t"})
        with urllib.request.urlopen(req, timeout=5) as r:
            page = json.loads(r.read())
        # Flat log: text includes both the ‹agent› transition marker and the content line
        assert "finished output" in page["text"]
        assert page.get("unavailable") is not True
        assert "speaker_at_start" in page
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
        st, body = _req("GET", base + f"/status?owner_id={OWNER}")
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
        conn.request("GET", f"/status?owner_id={OWNER}")          # NO X-Nelix-Token header
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 401
    finally:
        server.shutdown(); server.server_close()
    assert "unauthorized_peer" in buf.getvalue()


def test_unix_bind_refuses_an_over_long_socket_path_naming_the_cause(fake_manager, tmp_path):
    """An over-long AF_UNIX node must fail with a message that names the path, the byte count and
    the setting to change — not a bare OSError('AF_UNIX path too long') from inside bind().

    The check has to happen BEFORE the server is constructed: server_bind() unlinks any existing
    node before binding, so a late failure destroys a live daemon's socket on the way down.
    macOS allows 103 bytes; pytest's own tmp_path is already ~125, which is exactly how an
    operator-set NELIX_HOME reaches this state.
    """
    over = str(tmp_path / ("d" * 120) / "rpc.sock")
    assert len(over.encode()) > 103
    with pytest.raises(ValueError) as e:
        make_server(fake_manager, Transport.unix(over))
    msg = str(e.value)
    assert "cannot bind" in msg and "NELIX_HOME" in msg and str(len(over.encode())) in msg
