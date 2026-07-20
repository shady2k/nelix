"""nelix-9a4.6 review fix pass + nelix-9a4.4 update: sid shape validation."""
import threading
from urllib.parse import quote

import pytest

from tests.conftest import EXECUTOR, OWNER, make_spec, reserve_start, serve
from daemon import owner
from daemon.events import EventQueue
from daemon.launchers.base import ExecutorCapabilities
from daemon.manager import SessionManager
from daemon.session import RespondOutcome
from tests.test_rpc_server import _req

TRAVERSAL = "../../etc/passwd"


class _StubDriver:
    hook_capable = True


class _StubLauncher:
    capabilities = ExecutorCapabilities(isolation_class="host", can_attach=False)


class FakeSession:
    def __init__(self, sid, executor, *a, **k):
        self.sid = sid
        self.executor = executor
        self.task = self.cwd = None
        self.hook_secret = f"secret-{sid}"
        self.on_terminal = None
        self.dialog = None
        self._driver = _StubDriver()
        self._launcher = _StubLauncher()

    def start(self, task, cwd):
        self.task, self.cwd = task, cwd

    def snapshot(self):
        return {"session_id": self.sid, "executor": self.executor,
                "control_state": "busy", "task_delivery": "pending"}

    def screen(self, raw=False):
        return "SCREEN"

    def is_working(self):
        return False

    _cols = 80
    _rows = 24

    def stop(self):
        pass

    def observe(self): pass
    def last_observed(self): return 0.0
    def orphan_marked_ts(self): return None
    def mark_orphaned(self, grace): pass

    def respond(self, answer, decision_id=None):
        return RespondOutcome("resumed", seq=1, decision_id=decision_id,
                              answered_decision_id=decision_id, snapshot=self.snapshot())


@pytest.fixture
def daemon(store_and_ledger):
    store, ledger = store_and_ledger
    made = {}

    def session_factory(sid, executor, spec, events):
        s = FakeSession(sid, executor)
        made[sid] = s
        return s

    mgr = SessionManager({EXECUTOR: make_spec()}, EventQueue(), store,
                         session_factory=session_factory, concurrency_limit=10)
    srv, base = serve(mgr)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield base, mgr, made, ledger
    finally:
        srv.shutdown()


def _start(base, tmp_path, session_id):
    body = {"executor": EXECUTOR, "task": "t", "cwd": str(tmp_path), "owner_id": OWNER,
            "session_id": session_id}
    st, b = _req("POST", base + "/start", body=body)
    assert st == 200, b
    return b["session_id"]


# ============================================================ bad-shape sid: 400 before any read

@pytest.mark.parametrize("make_req", [
    lambda base, sid: _req("GET", base + f"/status?owner_id={OWNER}&session_id={sid}"),
    lambda base, sid: _req("GET", base + f"/dialog?owner_id={OWNER}&session_id={sid}"),
    lambda base, sid: _req("GET", base + f"/screen?owner_id={OWNER}&session_id={sid}"),
    lambda base, sid: _req("GET", base + f"/capabilities?owner_id={OWNER}&sid={sid}"),
    lambda base, sid: _req("GET", base + f"/wait?owner_id={OWNER}&after_seq=0&session_id={sid}"),
], ids=["status", "dialog", "screen", "capabilities", "wait"])
def test_traversal_sid_is_400_and_never_reaches_the_owner_check(daemon, monkeypatch, make_req):
    base, mgr, made, ledger = daemon

    def boom(*a, **k):
        raise AssertionError("owner.owns_session must never be called for a bad-shape sid")
    monkeypatch.setattr(owner, "owns_session", boom)

    st, b = make_req(base, TRAVERSAL)
    assert st == 400, (st, b)
    assert b["error"]["code"] == "invalid_session_id"
    assert b["error"]["retryable"] is False
    assert "message" in b["error"]


BAD_SHAPES = [TRAVERSAL, "s-BADUPPER", "s-deadbeef\n"]


@pytest.mark.parametrize("make_req", [
    lambda base, sid: _req("GET", base + f"/wait?owner_id={OWNER}&after_seq=0&"
                                          f"session_id={quote(sid, safe='')}"),
    lambda base, sid: _req("POST", base + "/respond",
                           body={"session_id": sid, "owner_id": OWNER, "answer": "yes"}),
    lambda base, sid: _req("POST", base + "/stop", body={"session_id": sid, "owner_id": OWNER}),
], ids=["wait", "respond", "stop"])
@pytest.mark.parametrize("bad_sid", BAD_SHAPES, ids=["traversal", "upper", "trailing_newline"])
def test_wait_respond_stop_bad_shape_sid_is_400_and_never_reaches_the_owner_check(
        daemon, monkeypatch, make_req, bad_sid):
    base, mgr, made, ledger = daemon
    def boom(*a, **k):
        raise AssertionError("owner.owns_session must never be called for a bad-shape sid")
    monkeypatch.setattr(owner, "owns_session", boom)
    st, b = make_req(base, bad_sid)
    assert st == 400, (st, b)
    assert b["error"]["code"] == "invalid_session_id"
    assert b["error"]["retryable"] is False


def test_dialog_traversal_sid_performs_no_out_of_root_read(daemon, monkeypatch):
    base, mgr, made, ledger = daemon
    def boom(*a, **k):
        raise AssertionError("owner.owns_session must never be called for a bad-shape sid")
    monkeypatch.setattr(owner, "owns_session", boom)
    st, b = _req("GET", base + f"/dialog?owner_id={OWNER}&session_id={TRAVERSAL}")
    assert st == 400
    assert b["error"]["code"] == "invalid_session_id"


def test_restart_traversal_sid_is_400_and_never_reaches_the_owner_check(daemon, monkeypatch):
    base, mgr, made, ledger = daemon
    def boom(*a, **k):
        raise AssertionError("owner.session_owned_by must never be called for a bad-shape sid")
    monkeypatch.setattr(owner, "session_owned_by", boom)
    st, b = _req("POST", base + "/restart",
                 body={"session_id": TRAVERSAL, "owner_id": OWNER,
                       "new_session_id": "s-" + "c" * 32})
    assert st == 400, (st, b)
    assert b["error"]["code"] == "invalid_session_id"
    assert b["error"]["retryable"] is False


@pytest.mark.parametrize("path,body", [
    ("/hook/{}", {"hook_event_name": "Stop"}),
    ("/message/{}", {"kind": "note", "summary": "x"}),
])
def test_traversal_sid_on_the_executor_plane_is_400_not_401_or_500(daemon, monkeypatch, path, body):
    base, mgr, made, ledger = daemon
    def boom(*a, **k):
        raise AssertionError("manager.get must never be called for a bad-shape sid")
    monkeypatch.setattr(mgr, "get", boom)
    st, b = _req("POST", base + path.format(TRAVERSAL), body=body)
    assert st == 400, (st, b)
    assert b["error"]["code"] == "invalid_session_id"


def test_capabilities_bad_shape_sid_never_reaches_the_manager(daemon, monkeypatch):
    base, mgr, made, ledger = daemon
    def boom(*a, **k):
        raise AssertionError("manager.capabilities must never be called for a bad-shape sid")
    monkeypatch.setattr(mgr, "capabilities", boom)
    st, b = _req("GET", base + f"/capabilities?owner_id={OWNER}&sid={TRAVERSAL}")
    assert st == 400
    assert b["error"]["code"] == "invalid_session_id"


# ============================================================ real ids: regression guard

def test_router_assigned_id_passes_every_validated_route(daemon, tmp_path):
    """nelix-9a4.4: all session_ids are router-assigned. The shape validator must accept them."""
    base, mgr, made, ledger = daemon
    sid = _start(base, tmp_path, reserve_start(ledger))
    assert _req("GET", base + f"/status?owner_id={OWNER}&session_id={sid}")[0] == 200
    assert _req("GET", base + f"/screen?owner_id={OWNER}&session_id={sid}")[0] == 200
    assert _req("GET", base + f"/capabilities?owner_id={OWNER}&sid={sid}")[0] == 200
    st, b = _req("GET", base + f"/dialog?owner_id={OWNER}&session_id={sid}")
    assert st in (200, 404), (st, b)
    assert st != 400
    st, b = _req("POST", base + "/hook/" + sid, body={"hook_event_name": "Stop"},
                 headers={"X-Nelix-Hook-Secret": made[sid].hook_secret})
    assert st != 400
    st, b = _req("POST", base + "/message/" + sid, body={"kind": "note", "summary": "x"},
                 headers={"X-Nelix-Hook-Secret": made[sid].hook_secret})
    assert st != 400
    mgr._events.publish(sid, EXECUTOR, "waiting_for_user", "y/n?", "waiting_for_user")
    st, b = _req("GET", base + f"/wait?owner_id={OWNER}&after_seq=0&session_id={sid}")
    assert st == 200, (st, b)
    assert b["event"]["session_id"] == sid
    st, b = _req("POST", base + "/respond",
                 body={"session_id": sid, "owner_id": OWNER, "answer": "yes"})
    assert st == 200, (st, b)
    # restart: needs new_session_id (nelix-9a4.4)
    new_sid = reserve_start(ledger)
    st, b = _req("POST", base + "/restart",
                 body={"session_id": sid, "owner_id": OWNER, "force": True,
                       "new_session_id": new_sid})
    assert st != 400, (st, b)
    st, b = _req("POST", base + "/stop", body={"session_id": sid, "owner_id": OWNER})
    assert st != 400, (st, b)
