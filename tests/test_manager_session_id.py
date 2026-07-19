"""nelix-9a4.6 + nelix-9a4.4: session ids are ALWAYS router-assigned (spec §3).

SessionManager.start()/._spawn() REQUIRE a `session_id` — no default, no self-mint.
restart() REQUIRES `new_session_id` — no daemon-side minting.
"""
import re

import pytest
from conftest import EXECUTOR, OWNER, make_spec, reserve_start
from daemon.events import EventQueue
from daemon.manager import SessionIdInUse, SessionIdRejected, SessionManager


def _mgr(store_and_ledger, limit=5):
    store, ledger = store_and_ledger
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

    m = SessionManager(specs, q, store, session_factory=session_factory, concurrency_limit=limit)
    return m, captured, ledger


def test_start_requires_a_session_id(store_and_ledger):
    """session_id is REQUIRED — no default, no self-mint."""
    m, _, ledger = _mgr(store_and_ledger)
    with pytest.raises(TypeError):
        m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER)  # missing session_id


def test_start_honors_a_router_assigned_legacy_shaped_id(store_and_ledger):
    m, captured, ledger = _mgr(store_and_ledger)
    sid = reserve_start(ledger)
    out = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id=sid)
    assert out.session_id == sid
    assert captured[0].sid == sid


def test_start_honors_a_router_assigned_wide_uuid4_shaped_id(store_and_ledger):
    m, _, ledger = _mgr(store_and_ledger)
    # The router mints full UUID4 hex (32 chars); reserve_start gives us one
    sid = reserve_start(ledger)
    assert len(sid) == 34  # s- + 32 hex
    out = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id=sid)
    assert out.session_id == sid


@pytest.mark.parametrize("bad", [
    "",
    "s-",
    "s-xyz12345",
    "../../etc/passwd",
    "s-abc/def",
    "no-prefix12345678",
    "s-" + "a" * 65,
    "s-ABCDEF12",
    "s-deadbeef\n",
    "s-deadbeef ",
    "s-dead\nbeef",
])
def test_start_rejects_bad_shaped_session_id(store_and_ledger, bad):
    m, captured, ledger = _mgr(store_and_ledger)
    with pytest.raises(SessionIdRejected):
        m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id=bad)
    assert captured == []


def test_start_rejects_a_live_session_id_collision(store_and_ledger):
    m, _, ledger = _mgr(store_and_ledger, limit=5)
    sid = reserve_start(ledger)
    out0 = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id=sid)
    assert out0.session_id == sid
    with pytest.raises(SessionIdInUse):
        m.start(EXECUTOR, "t2", "/tmp", owner_id=OWNER, session_id=sid)  # same id collides


def test_start_rejects_an_on_disk_session_id_collision(store_and_ledger):
    import paths
    m, _, ledger = _mgr(store_and_ledger)
    (paths.sessions_root() / "s-33334444").mkdir(parents=True)
    with pytest.raises(SessionIdInUse):
        m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id="s-33334444")


def test_start_rejected_session_id_does_not_consume_a_slot(store_and_ledger):
    m, captured, ledger = _mgr(store_and_ledger, limit=1)
    with pytest.raises(SessionIdRejected):
        m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id="bad id")
    # The slot is still free: a follow-up start with a valid id succeeds.
    sid = reserve_start(ledger)
    out = m.start(EXECUTOR, "t2", "/tmp", owner_id=OWNER, session_id=sid)
    assert out.session_id == sid
    assert captured[0].started == "t2"


def test_restart_requires_new_session_id(store_and_ledger):
    """restart() requires new_session_id — always router-assigned."""
    m, _, ledger = _mgr(store_and_ledger, limit=2)
    sid = reserve_start(ledger)
    out0 = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER, session_id=sid)
    new_sid = reserve_start(ledger)
    out = m.restart(out0.session_id, new_session_id=new_sid, owner_id=OWNER, force=True)
    assert re.match(r"^s-[0-9a-f]{32}$", out.session_id)
    assert out.session_id == new_sid
    assert out.session_id != out0.session_id
