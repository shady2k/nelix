"""nelix-9a4.6 review fix pass, finding #3: "/start alone shape-checked a caller-supplied
session_id; every other route that takes one read straight past it onto
`paths.sessions_root() / sid` (/dialog's transcript read, /restart's crashed-session disk-meta
fallback, and the owner-gate every other route relies on)." Codex demonstrated
`/dialog?session_id=../../x` and a traversal `/restart` reading `owner.json`/`meta.json` outside
the sessions root.

Fix: ONE shared validator (`daemon.manager.validate_session_id_shape`, the same shape `/start`
already enforced) is now applied at the RPC layer to every route that takes a caller-supplied
session id used as a path component: /status, /dialog, /screen, /restart, /hook/<sid>,
/message/<sid> -- plus /capabilities (finding #4's blank-sid fix reuses the same validator).

This file uses a REAL SessionManager + REAL make_server (like test_owner_isolation.py), never a
FakeManager stand-in for the routes under test here: the claim being proved is "a bad-shape sid is
refused BEFORE the filesystem/owner layer is ever touched", which only a real owner/manager stack
can falsify.
"""
import threading

import pytest

from conftest import EXECUTOR, OWNER, make_spec
from daemon import owner
from daemon.events import EventQueue
from daemon.launchers.base import ExecutorCapabilities
from daemon.manager import SessionManager
from daemon.rpc_server import make_server
from daemon.transport import Transport
from test_rpc_server import _req

_PORT = iter(range(9200, 9300))

TRAVERSAL = "../../etc/passwd"
WIDE_ID = "s-" + "b" * 32          # router-assigned wide id (spec §3): must still pass everywhere


class _StubDriver:
    hook_capable = True


class _StubLauncher:
    capabilities = ExecutorCapabilities(isolation_class="host", can_attach=False)


class FakeSession:
    """No PTY. Real disk state (owner.json etc.) still comes from the REAL manager/owner code --
    only the process is faked, exactly as test_owner_isolation.py's FakeSession does."""

    def __init__(self, sid, executor, *a, **k):
        self.sid = sid
        self.executor = executor
        self.task = self.cwd = None
        self.hook_secret = f"secret-{sid}"
        self.on_terminal = None
        self.dialog = None                    # /dialog's live-session fallback checks this
        self._driver = _StubDriver()          # /capabilities reads hook_capable off this
        self._launcher = _StubLauncher()       # /capabilities reads .capabilities off this

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


@pytest.fixture
def daemon():
    made = {}

    def session_factory(sid, executor, spec, events):
        s = FakeSession(sid, executor)
        made[sid] = s
        return s

    mgr = SessionManager({EXECUTOR: make_spec()}, EventQueue(),
                         session_factory=session_factory, concurrency_limit=10)
    port = next(_PORT)
    srv = make_server(mgr, Transport.tcp("127.0.0.1", port, "t"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", mgr, made
    finally:
        srv.shutdown()


def _start(base, tmp_path, session_id=None):
    body = {"executor": EXECUTOR, "task": "t", "cwd": str(tmp_path), "owner_id": OWNER}
    if session_id is not None:
        body["session_id"] = session_id
    st, b = _req("POST", base + "/start", body=body)
    assert st == 200, b
    return b["session_id"]


# ============================================================ bad-shape sid: 400 before any read

@pytest.mark.parametrize("make_req", [
    lambda base, sid: _req("GET", base + f"/status?owner_id={OWNER}&session_id={sid}"),
    lambda base, sid: _req("GET", base + f"/dialog?owner_id={OWNER}&session_id={sid}"),
    lambda base, sid: _req("GET", base + f"/screen?owner_id={OWNER}&session_id={sid}"),
    lambda base, sid: _req("GET", base + f"/capabilities?owner_id={OWNER}&sid={sid}"),
], ids=["status", "dialog", "screen", "capabilities"])
def test_traversal_sid_is_400_and_never_reaches_the_owner_check(daemon, monkeypatch, make_req):
    base, mgr, made = daemon

    def boom(*a, **k):
        raise AssertionError("owner.owns_session must never be called for a bad-shape sid")
    monkeypatch.setattr(owner, "owns_session", boom)

    st, b = make_req(base, TRAVERSAL)
    assert st == 400, (st, b)
    assert b["error"]["code"] == "invalid_session_id"
    assert b["error"]["retryable"] is False
    assert "message" in b["error"]


def test_dialog_traversal_sid_performs_no_out_of_root_read(daemon, monkeypatch):
    # The concrete Codex exploit: /dialog?session_id=../../x reading ../../x/owner.json +
    # transcript.jsonl. DialogReader must never even be constructed for a bad-shape sid.
    base, mgr, made = daemon

    def boom(*a, **k):
        raise AssertionError("owner.owns_session must never be called for a bad-shape sid")
    monkeypatch.setattr(owner, "owns_session", boom)

    st, b = _req("GET", base + f"/dialog?owner_id={OWNER}&session_id={TRAVERSAL}")
    assert st == 400
    assert b["error"]["code"] == "invalid_session_id"


def test_restart_traversal_sid_is_400_and_never_reaches_the_owner_check(daemon, monkeypatch):
    # Codex: /restart reading out-of-root owner.json/meta.json (the crashed-session disk-meta
    # fallback in `_restart_source`) and possibly spawning from what it finds.
    base, mgr, made = daemon

    def boom(*a, **k):
        raise AssertionError("owner.session_owned_by must never be called for a bad-shape sid")
    monkeypatch.setattr(owner, "session_owned_by", boom)

    st, b = _req("POST", base + "/restart", body={"session_id": TRAVERSAL, "owner_id": OWNER})
    assert st == 400, (st, b)
    assert b["error"]["code"] == "invalid_session_id"
    assert b["error"]["retryable"] is False


@pytest.mark.parametrize("path,body", [
    ("/hook/{}", {"hook_event_name": "Stop"}),
    ("/message/{}", {"kind": "note", "summary": "x"}),
])
def test_traversal_sid_on_the_executor_plane_is_400_not_401_or_500(daemon, monkeypatch, path, body):
    # /hook and /message are exempt from owner-gating (per-session secret instead), but a
    # caller-supplied sid is still a path COMPONENT of the URL itself and gets the same shape
    # check (finding #3: "parse the sid first for path-prefix routes, then validate").
    base, mgr, made = daemon

    def boom(*a, **k):
        raise AssertionError("manager.get must never be called for a bad-shape sid")
    monkeypatch.setattr(mgr, "get", boom)

    st, b = _req("POST", base + path.format(TRAVERSAL), body=body)
    assert st == 400, (st, b)
    assert b["error"]["code"] == "invalid_session_id"


def test_capabilities_bad_shape_sid_never_reaches_the_manager(daemon, monkeypatch):
    base, mgr, made = daemon

    def boom(*a, **k):
        raise AssertionError("manager.capabilities must never be called for a bad-shape sid")
    monkeypatch.setattr(mgr, "capabilities", boom)

    st, b = _req("GET", base + f"/capabilities?owner_id={OWNER}&sid={TRAVERSAL}")
    assert st == 400
    assert b["error"]["code"] == "invalid_session_id"


# ============================================================ real ids: regression guard

def test_self_minted_id_still_passes_every_validated_route(daemon, tmp_path):
    # CRITICAL (per the review): the shared validator must not reject what /start already mints.
    base, mgr, made = daemon
    sid = _start(base, tmp_path)
    assert _req("GET", base + f"/status?owner_id={OWNER}&session_id={sid}")[0] == 200
    assert _req("GET", base + f"/screen?owner_id={OWNER}&session_id={sid}")[0] == 200
    assert _req("GET", base + f"/capabilities?owner_id={OWNER}&sid={sid}")[0] == 200
    st, b = _req("GET", base + f"/dialog?owner_id={OWNER}&session_id={sid}")
    assert st in (200, 404), (st, b)   # 404 only if no transcript/live dialog yet -- never 400
    assert st != 400
    st, b = _req("POST", base + "/hook/" + sid, body={"hook_event_name": "Stop"},
                 headers={"X-Nelix-Hook-Secret": made[sid].hook_secret})
    assert st != 400
    st, b = _req("POST", base + "/message/" + sid, body={"kind": "note", "summary": "x"},
                 headers={"X-Nelix-Hook-Secret": made[sid].hook_secret})
    assert st != 400


def test_router_assigned_wide_id_still_passes_every_validated_route(daemon, tmp_path):
    # spec §3's wide (full UUID4 hex) id must be just as acceptable as the legacy 8-hex self-mint.
    base, mgr, made = daemon
    sid = _start(base, tmp_path, session_id=WIDE_ID)
    assert sid == WIDE_ID
    assert _req("GET", base + f"/status?owner_id={OWNER}&session_id={sid}")[0] == 200
    assert _req("GET", base + f"/screen?owner_id={OWNER}&session_id={sid}")[0] == 200
    assert _req("GET", base + f"/capabilities?owner_id={OWNER}&sid={sid}")[0] == 200
    st, b = _req("GET", base + f"/dialog?owner_id={OWNER}&session_id={sid}")
    assert st != 400, (st, b)
    st, b = _req("POST", base + "/restart", body={"session_id": sid, "owner_id": OWNER, "force": True})
    assert st != 400, (st, b)
