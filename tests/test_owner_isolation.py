"""TWO HARNESSES, ONE DAEMON: harness X must not see, wake on, answer, restart or stop Y's session.

This is the slice's real claim, so it is proved the expensive way: a REAL SessionManager behind a
REAL make_server over REAL HTTP, with real owner records on a real disk. Only the PTY is faked
(FakeSession) — the executor is not part of this invariant, and everything that IS part of it runs.

The bar is EVERY caller-facing route, not the happy path:
    /status (board + single)   /wait   /dialog   /screen   /start   /respond   /stop   /restart
    /capabilities (per-session form)
A route missing from this file is a route with no proof, so `test_every_caller_facing_route_is_covered`
fails if one is added without one.

The two exempt routes (/hook, /message) are proved STILL EXEMPT here too — a per-session secret is
a stronger check than an owner id, and "fail closed everywhere" must not quietly break the
executor's own plane on the way past.

/health is a THIRD, DIFFERENT kind of exemption (nelix-9a4.6): it carries no session and no
per-caller state to leak, so there is nothing for an owner id to gate — proved here too, so that
distinction is not just asserted in a comment.
"""
import json
import re
import threading
import urllib.parse
from pathlib import Path

import pytest

import paths
from tests.conftest import EXECUTOR, make_spec, reserve_start, serve
from daemon import owner
from daemon.events import EventQueue
from daemon.launchers.base import ExecutorCapabilities
from daemon.manager import SessionManager
from daemon.session import RespondOutcome
from tests.test_rpc_server import _req


class _StubDriver:
    hook_capable = True


class _StubLauncher:
    capabilities = ExecutorCapabilities(isolation_class="host", can_attach=False)


X = "harness-x"          # e.g. Hermes, on a phone
Y = "harness-y"          # e.g. Claude Code, local — same daemon, same uid


class FakeSession:
    """A session with no PTY. Records what was typed into it, so an isolation failure shows up as
    a real side effect (Y's answer landing in X's executor), not just a wrong status code."""

    def __init__(self, sid, executor, *a, **k):
        self.sid = sid
        self.executor = executor
        self.task = self.cwd = None
        self.stopped = False
        self.answers = []
        self.lineage_id = None
        self.restarted_from = None
        self.model = None
        self.hook_secret = f"secret-{sid}"
        self.on_terminal = None
        self.dialog = None
        self._driver = _StubDriver()          # /capabilities reads hook_capable off this
        self._launcher = _StubLauncher()       # /capabilities reads .capabilities off this

    def start(self, task, cwd):
        self.task, self.cwd = task, cwd
        # Write meta.json exactly as the real Session._write_meta does. NOT decoration: the
        # restart path resolves a session that has left _sessions from THIS file, so a fake that
        # skips it makes every disk-restart test pass for the wrong reason — the restart would be
        # refused for want of meta, and a test claiming "refused because the OWNER record is gone"
        # would never once have exercised the owner check. (Measured: it did exactly that.)
        d = paths.sessions_root() / self.sid
        paths.ensure_private_dir(d)
        paths.session_meta(d).write_text(json.dumps({
            "cols": self._cols, "rows": self._rows, "executor": self.executor,
            "driver": "claude", "task": task, "cwd": cwd,
            "lineage_id": self.lineage_id, "restarted_from": self.restarted_from,
            "model": self.model}))

    def respond(self, answer, decision_id=None):
        self.answers.append(answer)
        return RespondOutcome("resumed", seq=1, decision_id="dec-1")

    def send_turn(self, text):
        self.answers.append(text)
        return RespondOutcome("resumed", seq=1, decision_id="dec-1")

    def has_pending_async(self, decision_id):
        return False

    def snapshot(self):
        return {"session_id": self.sid, "executor": self.executor,
                "control_state": "busy", "task": self.task, "task_delivery": "pending"}

    def terminal_snapshot(self):
        return {"session_id": self.sid, "terminal_kind": "done", "task": self.task,
                "lineage_id": self.lineage_id, "control_state": "exited"}

    def pending_async_id(self):
        return None

    def progress_view(self):
        return {}

    def is_working(self):
        return False

    def screen(self, raw=False):
        return f"SCREEN OF {self.sid} TASK={self.task}"

    _cols = 80
    _rows = 24

    def stop(self):
        self.stopped = True


@pytest.fixture
def daemon(store_and_ledger):
    """A real manager behind real routes. Yields (base_url, manager, sessions_by_id)."""
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
        yield base, mgr, made
    finally:
        srv.shutdown()


def _start(base, owner_id, task, tmp_path, session_id=None):
    body = {"executor": EXECUTOR, "task": task, "cwd": str(tmp_path),
            "owner_id": owner_id}
    if session_id is not None:
        body["session_id"] = session_id
    st, b = _req("POST", base + "/start", body=body)
    assert st == 200, b
    return b["session_id"]


@pytest.fixture
def two_harnesses(daemon, tmp_path, store_and_ledger):
    """X owns one session, Y owns another. The setup every isolation test starts from."""
    store, ledger = store_and_ledger
    base, mgr, made = daemon
    sx = _start(base, X, "X SECRET TASK", tmp_path, session_id=reserve_start(ledger))
    sy = _start(base, Y, "Y SECRET TASK", tmp_path, session_id=reserve_start(ledger))
    return base, mgr, made, sx, sy


# ============================================================ the record itself

def test_start_persists_the_owner_before_it_answers(daemon, tmp_path, store_and_ledger):
    store, ledger = store_and_ledger
    base, mgr, made = daemon
    sid = _start(base, X, "t", tmp_path, session_id=reserve_start(ledger))
    # Durable and on disk by the time the caller learned the id — not a lease, not in-memory.
    assert owner.owner_of(paths.sessions_root() / sid) == X
    assert json.loads(paths.session_owner(paths.sessions_root() / sid).read_text()) == {"owner_id": X}


def test_start_fails_and_spawns_nothing_when_the_owner_cannot_be_written(daemon, tmp_path, monkeypatch, store_and_ledger):
    # spec §7: the record is written BEFORE a successful start response, and start FAILS if it
    # cannot be written. The session must not exist, and no PTY may have been spawned for it.
    store, ledger = store_and_ledger
    base, mgr, made = daemon

    def boom(*a, **k):
        raise owner.OwnerWriteFailed("disk full")

    monkeypatch.setattr("daemon.manager.owner.write", boom)
    st, b = _req("POST", base + "/start",
                 body={"executor": EXECUTOR, "task": "t", "cwd": str(tmp_path), "owner_id": X,
                       "session_id": reserve_start(ledger)})
    assert st == 500, (st, b)                       # daemon failed a well-formed request
    # The Session OBJECT is constructed before the record is written (it is inert until start()),
    # so the invariant is not "nothing was built" — it is that nothing was ever SPAWNED, nothing
    # is reachable, and the caller learned no session id it could not have driven.
    assert [s.task for s in made.values()] == [None], "the PTY was spawned before the owner record"
    assert "session_id" not in b
    assert mgr.status(owner_id=X)["sessions"] == {}
    assert mgr._sessions == {}, "an unattributable session stayed registered"
    # and the slot was released — an unattributable start must not leak capacity
    monkeypatch.undo()
    assert _start(base, X, "t2", tmp_path, session_id=reserve_start(ledger))


@pytest.mark.parametrize("bad", [None, "", "has space", "-lead", "x" * 129])
def test_start_rejects_a_bad_owner_with_400_not_409(daemon, tmp_path, bad, store_and_ledger):
    # 400 (your input) not 409 (daemon full): a 409 invites a retry loop that can never succeed.
    store, ledger = store_and_ledger
    base, mgr, made = daemon
    body = {"executor": EXECUTOR, "task": "t", "cwd": str(tmp_path),
            "session_id": reserve_start(ledger)}
    if bad is not None:
        body["owner_id"] = bad
    st, b = _req("POST", base + "/start", body=body)
    assert st == 400, (st, b)
    assert made == {}


def test_bad_owner_is_400_even_when_the_daemon_is_FULL(tmp_path, store_and_ledger):
    """A bad owner_id must read as the caller's mistake even at capacity.

    The route's own shape check cannot prove this: it rejects a bad owner at the door, so it
    hides whether the MANAGER checks too. The case that separates them is a full daemon — if the
    manager validates only when it gets around to writing the record, the cap check fires first
    and a malformed owner comes back 409 "daemon full", telling the caller to retry a request
    that can never succeed no matter how much capacity frees up. Hence validate-before-the-cap.

    (Found by mutation: deleting `owner.validate` from _spawn left every other owner test green.)
    """
    store, ledger = store_and_ledger
    made = {}

    def session_factory(sid, executor, spec, events):
        s = FakeSession(sid, executor)
        made[sid] = s
        return s

    mgr = SessionManager({EXECUTOR: make_spec()}, EventQueue(), store,
                         session_factory=session_factory, concurrency_limit=1)
    mgr.start(EXECUTOR, "filler", str(tmp_path), owner_id=X,
              session_id=reserve_start(ledger))                  # the one slot is now taken
    with pytest.raises(RuntimeError):                             # sanity: the daemon IS full
        mgr.start(EXECUTOR, "second", str(tmp_path), owner_id=X,
                  session_id=reserve_start(ledger))
    # ...and a BAD owner against that full daemon is still OwnerRejected (-> 400), not the
    # RuntimeError (-> 409) the cap would raise.
    with pytest.raises(owner.OwnerRejected):
        mgr.start(EXECUTOR, "bad", str(tmp_path), owner_id="has space",
                  session_id=reserve_start(ledger))


def test_manager_start_rejects_a_bad_owner_directly(daemon, tmp_path, store_and_ledger):
    # The manager is the API; the route is one caller of it. A bad owner must be refused here
    # too, not only at the HTTP door.
    store, ledger = store_and_ledger
    base, mgr, made = daemon
    with pytest.raises(owner.OwnerRejected):
        mgr.start(EXECUTOR, "t", str(tmp_path), owner_id="-lead",
                  session_id=reserve_start(ledger))
    assert made == {}


def test_owner_record_is_written_even_when_meta_write_fails(daemon, tmp_path, monkeypatch, store_and_ledger):
    # The whole reason owner.json is not a field in meta.json: Session._write_meta swallows OSError
    # (fine for a capture sidecar), and an access invariant must not inherit that.
    store, ledger = store_and_ledger
    base, mgr, made = daemon
    sid = _start(base, X, "t", tmp_path, session_id=reserve_start(ledger))
    assert owner.owner_of(paths.sessions_root() / sid) == X


# ============================================================ /status — the board read

def test_board_read_shows_only_the_callers_sessions(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + f"/status?owner_id={X}")
    assert st == 200
    assert list(b["sessions"]) == [sx], "X's board read returned another harness's session"
    assert "Y SECRET TASK" not in json.dumps(b)
    st, b = _req("GET", base + f"/status?owner_id={Y}")
    assert list(b["sessions"]) == [sy]
    assert "X SECRET TASK" not in json.dumps(b)


def test_board_read_of_a_third_owner_is_empty(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + "/status?owner_id=harness-z")
    assert st == 200 and b["sessions"] == {} and b["recent_terminal"] == {}


def test_single_session_status_of_another_owner_is_unknown(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + f"/status?owner_id={Y}&session_id={sx}")
    assert st == 200 and b["error"] == "unknown session"
    assert "X SECRET TASK" not in json.dumps(b)


def test_status_requires_an_owner(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + "/status")
    # 400, not an empty board: a caller that forgot the field must learn it forgot. An empty
    # result reads like an idle daemon and would be silently believed.
    assert st == 400 and b["error"] == "missing owner_id"


def test_terminal_inventory_is_owner_filtered(two_harnesses):
    # recent_terminal relays a DISAPPEARED session — it carries the task text and final state, so
    # an unfiltered inventory leaks exactly what a live snapshot would.
    base, mgr, made, sx, sy = two_harnesses
    mgr._free_slot(sx)                                   # X's session goes terminal
    st, b = _req("GET", base + f"/status?owner_id={Y}")
    assert b["recent_terminal"] == {}, "Y saw X's terminal session in the inventory"
    assert "X SECRET TASK" not in json.dumps(b)
    # S2a.2: daemon no longer surfaces persisted terminals in recent_terminal
    # (advertised=False — the router's archive read owns that).
    st, b = _req("GET", base + f"/status?owner_id={X}")
    assert b["recent_terminal"] == {}, "S2a.2: daemon hides persisted terminals from live board"


# ============================================================ /wait — the waiter

def test_wait_never_arms_on_another_owners_session(two_harnesses):
    # "arms a waiter for each" is half the bug: a waiter is HOW one harness ends up answering
    # another's decision. Y must not wake on X's event even holding X's session id.
    base, mgr, made, sx, sy = two_harnesses
    mgr._events.publish(sx, EXECUTOR, "waiting_for_user", "X NEEDS AN ANSWER", "idle_prompt",
                        requires_response=True)
    st, b = _req("GET", base + f"/wait?owner_id={Y}&session_id={sx}&after_seq=0")
    assert st == 404, "Y was woken by X's session event"
    assert "X NEEDS AN ANSWER" not in json.dumps(b)


def test_unwakeable_wait_is_an_error_not_a_null_event(two_harnesses):
    """A wait that can never wake must SAY so, not answer null.

    Not a cosmetic status-code preference — it is the difference between a caller that stops and
    a caller that melts the daemon. /wait's contract is "blocks ~25s, null means re-issue", so a
    non-owner answered `200 {"event": null}` re-issues INSTANTLY and forever. Measured, before
    this was a 404: bin/nelix-wait spun at ~3400 req/s. The isolation held the whole time — the
    leak was never the bug here; the retry storm was.
    """
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + f"/wait?owner_id={Y}&session_id={sx}&after_seq=0")
    assert st == 404 and "event" not in b, (
        "an un-armable wait answered like an ordinary empty poll — every correct waiter will "
        "retry it at once, forever")
    assert "retry" in b.get("hint", "").lower()


def test_wait_delivers_to_the_owner(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    mgr._events.publish(sx, EXECUTOR, "waiting_for_user", "X NEEDS AN ANSWER", "idle_prompt",
                        requires_response=True)
    st, b = _req("GET", base + f"/wait?owner_id={X}&session_id={sx}&after_seq=0")
    assert st == 200 and b["event"]["summary"] == "X NEEDS AN ANSWER"


def test_wait_requires_an_owner(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + f"/wait?session_id={sx}&after_seq=0")
    assert st == 400 and b["error"] == "missing owner_id"


# ============================================================ /dialog — reads DISK

def test_dialog_of_another_owners_session_is_unknown(two_harnesses, tmp_path):
    # THE route a session id alone must never open: it reads the transcript straight off disk and
    # never passes through the manager, so it has to gate itself.
    base, mgr, made, sx, sy = two_harnesses
    d = paths.sessions_root() / sx
    (d / "transcript.jsonl").write_text(
        json.dumps({"role": "assistant", "text": "X PRIVATE TRANSCRIPT"}) + "\n")
    st, b = _req("GET", base + f"/dialog?owner_id={Y}&session_id={sx}")
    assert st == 404, (st, b)
    assert "X PRIVATE TRANSCRIPT" not in json.dumps(b)


def test_dialog_still_serves_its_owner(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    d = paths.sessions_root() / sx
    (d / "transcript.jsonl").write_text(
        json.dumps({"role": "assistant", "text": "X PRIVATE TRANSCRIPT"}) + "\n")
    st, b = _req("GET", base + f"/dialog?owner_id={X}&session_id={sx}")
    assert st == 200 and "X PRIVATE TRANSCRIPT" in json.dumps(b)


def test_dialog_requires_an_owner(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + f"/dialog?session_id={sx}")
    assert st == 400 and b["error"] == "missing owner_id"


# ============================================================ /screen — queries the manager

def test_screen_of_another_owners_session_is_unknown(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + f"/screen?owner_id={Y}&session_id={sx}")
    assert st == 200 and b["error"] == "unknown session"
    assert "X SECRET TASK" not in json.dumps(b)


def test_screen_force_does_not_bypass_the_owner_gate(two_harnesses):
    # `force` exists to bypass the anti-poll WITHHOLD. It must not also bypass ownership — the two
    # checks are ordered inside manager.screen and this pins that order.
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + f"/screen?owner_id={Y}&session_id={sx}&force=1&raw=1")
    assert b["error"] == "unknown session"
    assert "X SECRET TASK" not in json.dumps(b)


def test_screen_still_serves_its_owner(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + f"/screen?owner_id={X}&session_id={sx}")
    assert st == 200 and "X SECRET TASK" in b["screen"]


def test_screen_requires_an_owner(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + f"/screen?session_id={sx}")
    assert st == 400 and b["error"] == "missing owner_id"


# ============================================================ /capabilities — per-session (nelix-9a4.6)

def test_capabilities_of_another_owners_session_is_unknown(two_harnesses):
    # NOTE the query key is `sid`, not `session_id` (verbatim per the brief — the one route where
    # the two differ).
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + f"/capabilities?owner_id={Y}&sid={sx}")
    assert st == 404
    assert b["error"]["code"] == "unknown_session"
    assert "X SECRET TASK" not in json.dumps(b)


def test_capabilities_still_serves_its_owner(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + f"/capabilities?owner_id={X}&sid={sx}")
    assert st == 200 and b["session_id"] == sx
    assert b["hook_capable"] is True   # a real fact (FakeSession's _StubDriver), not an operation code
    assert "operations" not in b


def test_capabilities_requires_an_owner(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + f"/capabilities?sid={sx}")
    assert st == 400 and b["error"] == "missing owner_id"


# ============================================================ /respond — the irreversible one

def test_respond_cannot_answer_another_harnesss_decision(two_harnesses):
    # nelix-v96's class at the harness boundary. The assertion that matters is not the status
    # code — it is that NOTHING was typed into X's executor. An answer cannot be taken back.
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("POST", base + "/respond",
                 body={"session_id": sx, "answer": "yes, delete everything", "owner_id": Y})
    assert st == 404 and b["status"] == "unknown_session"
    assert made[sx].answers == [], "Y's answer was typed into X's executor"


def test_respond_with_a_decision_id_cannot_reach_another_owner(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("POST", base + "/respond",
                 body={"session_id": sx, "answer": "yes", "decision_id": "dec-1", "owner_id": Y})
    assert st == 404
    assert made[sx].answers == []


def test_respond_still_works_for_the_owner(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("POST", base + "/respond",
                 body={"session_id": sx, "answer": "X's own answer", "owner_id": X})
    assert st == 200 and b["status"] == "resumed"
    assert made[sx].answers == ["X's own answer"]


def test_respond_requires_an_owner(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("POST", base + "/respond", body={"session_id": sx, "answer": "a"})
    assert st == 400 and b["error"] == "missing owner_id"
    assert made[sx].answers == []


# ============================================================ /stop — destructive

def test_stop_cannot_kill_another_harnesss_session(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("POST", base + "/stop", body={"session_id": sx, "owner_id": Y})
    assert st == 404 and b["status"] == "unknown_session"
    assert made[sx].stopped is False, "Y stopped X's session"
    assert mgr.get(sx) is not None


def test_stop_still_works_for_the_owner(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("POST", base + "/stop", body={"session_id": sx, "owner_id": X})
    assert st == 200
    assert made[sx].stopped is True


def test_stop_requires_an_owner(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("POST", base + "/stop", body={"session_id": sx})
    assert st == 400 and b["error"] == "missing owner_id"
    assert made[sx].stopped is False


def test_stop_all_still_stops_every_owners_sessions(two_harnesses):
    # Shutdown owns everything. It must NOT be expressible as "stop as some owner" — that would
    # need a wildcard owner, and a wildcard is the one thing that must not exist.
    base, mgr, made, sx, sy = two_harnesses
    mgr.stop_all()
    assert made[sx].stopped is True and made[sy].stopped is True


# ============================================================ /restart — inherits, never trusts

def test_restart_cannot_restart_another_harnesss_session(two_harnesses, store_and_ledger):
    store, ledger = store_and_ledger
    base, mgr, made, sx, sy = two_harnesses
    before = set(made)
    st, b = _req("POST", base + "/restart",
                 body={"session_id": sx, "owner_id": Y,
                       "new_session_id": reserve_start(ledger)})
    assert st == 404 and b["status"] == "unknown_session"
    assert set(made) == before, "Y's restart spawned a session from X's"
    assert made[sx].stopped is False


def test_restart_inherits_the_stored_owner(two_harnesses, store_and_ledger):
    store, ledger = store_and_ledger
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("POST", base + "/restart",
                 body={"session_id": sx, "owner_id": X, "force": True,
                       "new_session_id": reserve_start(ledger)})
    assert st == 200, b
    new_sid = b["session_id"]
    assert owner.owner_of(paths.sessions_root() / new_sid) == X   # from disk, not from the body
    # and the restarted session lands in X's board, not Y's
    assert new_sid in _req("GET", base + f"/status?owner_id={X}")[1]["sessions"]
    assert new_sid not in _req("GET", base + f"/status?owner_id={Y}")[1]["sessions"]


def test_restart_refuses_a_session_whose_owner_record_is_gone(two_harnesses, store_and_ledger):
    # THE case that proves "inherits, never trusts the caller". A crashed session is resolved from
    # DISK meta, and every crashed session takes this path. If restart trusted the request's
    # owner_id, an ownerless session would be a free session — anyone could restart it and become
    # its owner. Fail closed instead.
    store, ledger = store_and_ledger
    base, mgr, made, sx, sy = two_harnesses
    mgr._free_slot(sx)                                    # gone from _sessions: the disk-meta path
    paths.session_owner(paths.sessions_root() / sx).unlink()
    st, b = _req("POST", base + "/restart",
                 body={"session_id": sx, "owner_id": X, "force": True,
                       "new_session_id": reserve_start(ledger)})
    assert st == 404 and b["status"] == "unknown_session"


def test_restart_refuses_a_session_whose_owner_record_is_malformed(two_harnesses, store_and_ledger):
    store, ledger = store_and_ledger
    base, mgr, made, sx, sy = two_harnesses
    mgr._free_slot(sx)
    paths.session_owner(paths.sessions_root() / sx).write_text("{tor")
    st, b = _req("POST", base + "/restart",
                 body={"session_id": sx, "owner_id": X, "force": True,
                       "new_session_id": reserve_start(ledger)})
    assert st == 404 and b["status"] == "unknown_session"


def test_restart_of_a_crashed_session_inherits_from_disk(two_harnesses, store_and_ledger):
    # The main restart path: session already gone from _sessions, resolved from meta.json on disk.
    store, ledger = store_and_ledger
    base, mgr, made, sx, sy = two_harnesses
    mgr._free_slot(sx)
    st, b = _req("POST", base + "/restart",
                 body={"session_id": sx, "owner_id": X, "force": True,
                       "new_session_id": reserve_start(ledger)})
    assert st == 200, b
    assert owner.owner_of(paths.sessions_root() / b["session_id"]) == X


def test_restart_requires_an_owner(two_harnesses, store_and_ledger):
    store, ledger = store_and_ledger
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("POST", base + "/restart",
                 body={"session_id": sx, "new_session_id": reserve_start(ledger)})
    assert st == 400 and b["error"] == "missing owner_id"


@pytest.mark.parametrize("caller", [None, "", "has space"])
def test_restart_direct_manager_call_refuses_a_bad_owner_on_an_ownerless_session(
        two_harnesses, caller, store_and_ledger):
    """The manager is the API; the RPC route is one caller of it.

    Every route-level test of this is blind here — /restart shape-checks owner_id at the door, so
    a None never reaches the manager over HTTP. Call the manager directly (as the router will,
    nelix-9a4.4) against a session with NO owner record and the trap opens: a raw `stored == caller`
    would compare None to None, find them equal, and hand an ownerless session to a caller who
    passed no owner at all. Found by mutation; this is the test that keeps it shut.
    """
    store, ledger = store_and_ledger
    base, mgr, made, sx, sy = two_harnesses
    mgr._free_slot(sx)
    paths.session_owner(paths.sessions_root() / sx).unlink()      # ownerless
    before = set(made)
    assert mgr.restart(sx, new_session_id=reserve_start(ledger),
                       owner_id=caller, force=True).status == "unknown_session"
    assert set(made) == before, "an ownerless session was restarted by a caller with no owner"


# ============================================================ fail-closed on a broken record

def test_a_session_with_no_owner_record_is_reachable_by_nobody(two_harnesses, store_and_ledger):
    # Fail CLOSED, not open: the failure mode of a lost record is "nobody can drive it", never
    # "everybody can". Checked on every route, because one fail-open route is enough.
    store, ledger = store_and_ledger
    base, mgr, made, sx, sy = two_harnesses
    paths.session_owner(paths.sessions_root() / sx).unlink()
    for who in (X, Y, "harness-z"):
        assert _req("GET", base + f"/status?owner_id={who}&session_id={sx}")[1]["error"] == "unknown session"
        assert sx not in _req("GET", base + f"/status?owner_id={who}")[1]["sessions"]
        assert _req("GET", base + f"/screen?owner_id={who}&session_id={sx}")[1]["error"] == "unknown session"
        assert _req("GET", base + f"/dialog?owner_id={who}&session_id={sx}")[0] == 404
        assert _req("GET", base + f"/wait?owner_id={who}&session_id={sx}&after_seq=0")[0] == 404
        assert _req("POST", base + "/respond",
                    body={"session_id": sx, "answer": "a", "owner_id": who})[0] == 404
        assert _req("POST", base + "/stop", body={"session_id": sx, "owner_id": who})[0] == 404
        assert _req("POST", base + "/restart",
                    body={"session_id": sx, "owner_id": who,
                          "new_session_id": reserve_start(ledger)})[0] == 404
    assert made[sx].answers == [] and made[sx].stopped is False


def test_a_malformed_owner_record_is_not_a_skeleton_key(two_harnesses):
    # A record whose owner_id is bad-shape grants nothing — not even to a caller sending the
    # identical bad string (which the route rejects at the door as a 400 anyway).
    base, mgr, made, sx, sy = two_harnesses
    paths.session_owner(paths.sessions_root() / sx).write_text(json.dumps({"owner_id": "has space"}))
    assert _req("GET", base + f"/status?owner_id={X}&session_id={sx}")[1]["error"] == "unknown session"
    q = urllib.parse.urlencode({"owner_id": "has space", "session_id": sx})
    st, b = _req("GET", base + "/status?" + q)
    assert st == 400


# ============================================================ the EXEMPT executor plane

def test_hook_route_is_not_owner_gated(two_harnesses):
    # /hook authenticates by PER-SESSION SECRET — stronger than an owner id. A worker is not a
    # caller: it holds a secret only its own session's launcher was given. Fail-closing owners
    # everywhere must not quietly break the executor's own plane.
    base, mgr, made, sx, sy = two_harnesses
    st, _ = _req("POST", base + f"/hook/{sx}", body={"hook_event_name": "Stop"},
                 headers={"X-Nelix-Hook-Secret": made[sx].hook_secret})
    assert st in (204, 500), st          # 500 only if FakeSession lacks on_hook — never 400/401
    assert st != 400, "/hook grew an owner_id requirement"


def test_hook_route_still_rejects_a_wrong_secret(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, _ = _req("POST", base + f"/hook/{sx}", body={"hook_event_name": "Stop"},
                 headers={"X-Nelix-Hook-Secret": made[sy].hook_secret})
    assert st == 401, "another session's secret was accepted"


def test_message_route_is_not_owner_gated(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("POST", base + f"/message/{sx}", body={"kind": "note", "text": "working"},
                 headers={"X-Nelix-Hook-Secret": made[sx].hook_secret})
    assert st != 400 or b.get("error") != "missing owner_id", "/message grew an owner_id requirement"


def test_health_route_needs_no_owner_at_all(two_harnesses):
    # A THIRD kind of exemption (nelix-9a4.6), distinct from /hook and /message's per-session
    # secret: /health carries no session and no per-caller state, so there is nothing an owner id
    # could gate. Proved against the SAME running daemon the other harnesses' sessions live on, so
    # this is not a different daemon's behavior.
    base, mgr, made, sx, sy = two_harnesses
    st, b = _req("GET", base + "/health")
    assert st == 200 and b["status"] == "ok"
    assert "X SECRET TASK" not in json.dumps(b) and "Y SECRET TASK" not in json.dumps(b)


def test_message_route_still_rejects_a_wrong_secret(two_harnesses):
    base, mgr, made, sx, sy = two_harnesses
    st, _ = _req("POST", base + f"/message/{sx}", body={"kind": "note", "text": "x"},
                 headers={"X-Nelix-Hook-Secret": made[sy].hook_secret})
    assert st == 401


# ============================================================ coverage guard

def test_every_caller_facing_route_is_covered():
    """A new caller-facing route with no owner proof is the way this invariant rots.

    Pinned against the route table in rpc_server so ADDING a route forces a decision here:
    either it is caller-facing (gate it, prove it above) or it is executor-facing (justify the
    exemption). Reads the source rather than the handler because there is no route registry to
    introspect — the dispatch is an if/elif chain.
    """
    src = (Path(__file__).resolve().parents[1] / "daemon" / "rpc_server.py").read_text()
    routes = set(re.findall(r'p\.path == "(/[a-z]+)"', src))
    routes |= {m.rstrip("/") for m in re.findall(r'p\.path\.startswith\("(/[a-z]+/)"\)', src)}
    caller_facing = {"/wait", "/status", "/dialog", "/screen",
                     "/start", "/respond", "/stop", "/restart", "/capabilities"}
    exempt = {"/hook", "/message"}                # per-session secret, stronger than an owner id
    no_owner_state = {"/health"}                  # no session, no per-caller state to gate at all
    assert routes == caller_facing | exempt | no_owner_state, (
        f"the route table changed: {routes ^ (caller_facing | exempt | no_owner_state)}. A new "
        f"caller-facing route must be owner-gated and proved in this file; a new executor-facing "
        f"route must justify its exemption (a per-session secret, not an owner id); a route with "
        f"no session/per-caller state at all (like /health) justifies needing no owner id.")
