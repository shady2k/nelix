"""nelix-9a4.6 deliverable A: the generation's start endpoint accepts a router-assigned session id
(spec §3). SessionManager.start()/._spawn() thread an optional `session_id` through; omitted ->
self-mint, byte-identical to pre-feature (the self-mint FORMAT is deliberately left `s-<8hex>` —
see the brief — only the WIDE-id acceptance is new)."""
import re

import pytest
from conftest import EXECUTOR, OWNER, make_spec
from daemon.events import EventQueue
from daemon.manager import SessionIdInUse, SessionIdRejected, SessionManager


def _mgr(limit=5):
    specs = {EXECUTOR: make_spec()}
    q = EventQueue()
    captured = []

    class FakeSession:
        def __init__(self, sid, executor, *a, **k):
            self.sid = sid
            self.executor = executor
            self.started = None
            self.task = None
            self.cwd = None

        def start(self, task, cwd):
            self.started = task
            self.task = task
            self.cwd = cwd

        def snapshot(self):
            return {"session_id": self.sid, "executor": self.executor,
                    "control_state": "busy", "task_delivery": "pending"}

        def stop(self):
            pass

    def session_factory(sid, executor, spec, events):
        s = FakeSession(sid, executor)
        captured.append(s)
        return s

    m = SessionManager(specs, q, session_factory=session_factory, concurrency_limit=limit)
    return m, captured


def test_start_without_session_id_self_mints_the_legacy_shape():
    # Omitted (None, the default) -> today's exact self-mint format, unchanged.
    m, _ = _mgr()
    out = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER)
    assert re.match(r"^s-[0-9a-f]{8}$", out.session_id)


def test_start_honors_a_router_assigned_legacy_shaped_id():
    m, captured = _mgr()
    out = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id="s-deadbeef")
    assert out.session_id == "s-deadbeef"
    assert captured[0].sid == "s-deadbeef"


def test_start_honors_a_router_assigned_wide_uuid4_shaped_id():
    # spec §3: "Widen the id ... Use a full UUID/ULID." The future router mints a full UUID4 hex
    # (32 chars); the validator must accept it, not just the legacy 8-hex form.
    m, _ = _mgr()
    wide = "s-" + "a" * 32
    out = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id=wide)
    assert out.session_id == wide


@pytest.mark.parametrize("bad", [
    "",                       # empty
    "s-",                     # empty hex part
    "s-xyz12345",             # non-hex chars
    "../../etc/passwd",       # path traversal
    "s-abc/def",              # separator
    "no-prefix12345678",      # missing the s- prefix
    "s-" + "a" * 65,          # over the accepted length
    "s-ABCDEF12",             # uppercase hex rejected (charset is lowercase-only)
])
def test_start_rejects_bad_shaped_session_id(bad):
    m, captured = _mgr()
    with pytest.raises(SessionIdRejected):
        m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id=bad)
    assert captured == []          # no session created on a rejected id


def test_start_rejects_a_live_session_id_collision():
    m, _ = _mgr(limit=5)
    out0 = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id="s-11112222")
    assert out0.session_id == "s-11112222"
    with pytest.raises(SessionIdInUse):
        m.start(EXECUTOR, "t2", "/tmp", owner_id=OWNER, session_id="s-11112222")


def test_start_rejects_an_on_disk_session_id_collision(tmp_path):
    # A session that already exited leaves its directory on disk; a router-supplied id naming
    # that directory must not be silently reused/clobbered (spec §3), even though it is no
    # longer LIVE in the registry.
    import paths
    m, _ = _mgr()
    (paths.sessions_root() / "s-33334444").mkdir(parents=True)
    with pytest.raises(SessionIdInUse):
        m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id="s-33334444")


def test_start_rejected_session_id_does_not_consume_a_slot():
    m, captured = _mgr(limit=1)
    with pytest.raises(SessionIdRejected):
        m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id="bad id")
    # The slot is still free: a follow-up start (no explicit id) succeeds.
    out = m.start(EXECUTOR, "t2", "/tmp", owner_id=OWNER)
    assert out.session_id
    assert captured[0].started == "t2"


def test_restart_still_self_mints_never_taking_an_explicit_id():
    # restart() has no session_id parameter of its own — the OUT-OF-SCOPE list forbids any
    # router-side start-ledger / generation_id persistence in the restart path, and restart()
    # must keep self-minting exactly as before.
    m, _ = _mgr(limit=2)
    out0 = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER)
    out = m.restart(out0.session_id, owner_id=OWNER, force=True)
    assert re.match(r"^s-[0-9a-f]{8}$", out.session_id)
    assert out.session_id != out0.session_id
