"""Task 8a: the orchestrator-facing `nelix_status(include_progress)` surface.

Session.progress_view() (Task 2) and Session.snapshot()'s wake-point gate already exist; this
wires an EXPLICIT on-demand override through the whole stack so Hermes can ask for the bounded
progress-note list even while the agent is actively working, WITHOUT weakening the anti-poll
default (an active-working status call with no include_progress must stay exactly as boring as
today — nothing to read, nothing to poll for):

    nelix_status(include_progress) -> RpcClient.status -> GET /status?include_progress=1
        -> manager.status(session_id, include_progress=True) -> merges Session.progress_view()

Three layers are exercised: the manager (the actual merge/gate logic), the HTTP layer (query-param
threading), and the plugin tool (arg passthrough to RpcClient).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from daemon.events import EventQueue                         # noqa: E402
from daemon.manager import SessionManager                    # noqa: E402
from daemon.messages import ProgressNote                     # noqa: E402
from test_async_question import _demo_specs, _make_session   # noqa: E402
from test_plugin_wake_cursor import _setup                   # noqa: E402
from test_rpc_server import _req, _serve                     # noqa: E402


# ---------------------------------------------------------------------------
# Manager level: the actual merge/gate logic (real Session, real SessionManager)
# ---------------------------------------------------------------------------

def _manager_with_busy_session(tmp_path):
    sess, ev = _make_session(tmp_path, ["compiling…", "compiling…"])
    sess._loop()
    assert sess._decision is None and sess._state == "busy"    # active-working, no wake point
    mgr = SessionManager(_demo_specs(), ev, concurrency_limit=3,
                         session_retain=0, session_max_age_days=0)
    with mgr._lock:
        mgr._sessions[sess._id] = sess
    return mgr, sess


def test_manager_status_default_omits_progress_during_active_working(tmp_path):
    mgr, sess = _manager_with_busy_session(tmp_path)
    sess.append_progress_note(ProgressNote("step 1", None))
    snap = mgr.status(sess._id)                     # default: include_progress=False
    assert snap["control_state"] == "busy"
    assert "progress" not in snap and "progress_total" not in snap   # anti-poll unchanged


def test_manager_status_include_progress_returns_notes_during_active_working(tmp_path):
    mgr, sess = _manager_with_busy_session(tmp_path)
    sess.append_progress_note(ProgressNote("step 1", "detail"))
    snap = mgr.status(sess._id, include_progress=True)
    assert snap["control_state"] == "busy"           # still active-working -- NOT a wake point
    assert snap["progress"][-1]["summary"] == "step 1"
    assert snap["progress_total"] == 1 and snap["progress_retained"] == 1


def test_manager_status_all_sessions_board_include_progress(tmp_path):
    """The all-sessions board read (session_id=None) gets the same on/off toggle per-session."""
    mgr, sess = _manager_with_busy_session(tmp_path)
    sess.append_progress_note(ProgressNote("board view", None))
    board_default = mgr.status()
    assert "progress" not in board_default["sessions"][sess._id]
    board = mgr.status(include_progress=True)
    assert board["sessions"][sess._id]["progress"][-1]["summary"] == "board view"


# ---------------------------------------------------------------------------
# HTTP level: GET /status?include_progress=1 query-param threading
# ---------------------------------------------------------------------------

class _ProgressAwareManager:
    """Records what `include_progress` value it was called with, so the test can assert the
    query param actually reaches manager.status() rather than being silently dropped."""
    def __init__(self):
        self._events = EventQueue()
        self.seen = []

    def status(self, sid=None, include_progress=False):
        self.seen.append(include_progress)
        body = {"session_id": sid, "control_state": "busy"}
        if include_progress:
            body["progress"] = [{"progress_seq": 1, "summary": "hi"}]
            body["progress_total"] = 1
        return body


def test_rpc_status_default_omits_include_progress():
    m = _ProgressAwareManager()
    srv, base = _serve(m, __import__("io").StringIO())
    try:
        st, b = _req("GET", base + "/status?session_id=s1")
        assert st == 200 and m.seen[-1] is False
        assert "progress" not in b
    finally:
        srv.shutdown()


def test_rpc_status_include_progress_query_threads_through():
    m = _ProgressAwareManager()
    srv, base = _serve(m, __import__("io").StringIO())
    try:
        st, b = _req("GET", base + "/status?session_id=s1&include_progress=1")
        assert st == 200 and m.seen[-1] is True
        assert b["progress"][-1]["summary"] == "hi" and b["progress_total"] == 1
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------
# Plugin level: nelix_status arg passthrough to RpcClient.status
# ---------------------------------------------------------------------------

def test_nelix_status_include_progress_arg_passthrough(monkeypatch, tmp_path):
    seen = {}

    class C:
        def __init__(self, t): pass
        def status(self, sid=None, include_progress=False):
            seen["sid"] = sid
            seen["include_progress"] = include_progress
            return {"sessions": {}}

    _, ctx = _setup(monkeypatch, tmp_path, C)
    ctx.tools["nelix_status"]["handler"]({"include_progress": True})
    assert seen["include_progress"] is True

    ctx.tools["nelix_status"]["handler"]({})
    assert seen["include_progress"] is False       # default omitted -> passed through as False
