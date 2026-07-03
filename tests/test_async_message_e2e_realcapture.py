"""Task 9: end-to-end test for the executor<->orchestrator async-message feature, through the REAL
daemon components wired together the way production wires them — not a re-run of any single layer's
unit tests (those already exist: Session-level in test_async_question.py /
test_async_delivery_realcapture.py, HTTP-route-level against a FakeManager in test_message_route.py /
test_nelix_wrappers.py). This file is the seam NONE of those cover: a real `daemon.rpc_server.make_server`
bound to a real AF_UNIX socket, serving a real `daemon.manager.SessionManager` holding a real
`daemon.session.Session` (registered exactly as `Manager._spawn` wires a live one — `on_terminal` +
`deliver_turn` — the same idiom `test_async_delivery_realcapture.py` uses to avoid actually forking a
child process), driven end to end via `bin/nelix-question` / `bin/nelix-note` subprocesses and
`rpc_client.RpcClient` (the same client `__init__.py`'s `nelix_status`/`nelix_respond` tools use).

Busy/idle transitions are driven via REAL Claude Code hook events (`Session.on_hook` +
`Session._loop_once()`), the ACTUAL production mechanism for the hook-capable claude driver's
control_state (screen content is inert while hook_mode is active — `_loop_once`'s belief tick
self-gates on it) — never hand-set state. The scripted PTY's static frame is not hand-typed ASCII:
it is a real rendered excerpt from a committed capture (`tests/golden/claude/idle_prompt/bare-prompt.txt`),
matching this project's "no fabricated frames" testing discipline even though this particular frame's
content is not itself load-bearing for these assertions (control_state comes from hooks, not the
screen, while a hook is authoritative).

A genuinely NEW live `claude` capture of a real question/wake/answer/reconcile session is not
feasible inside an automated harness (it needs an interactive executor process) — that dogfooding is
the user's post-merge step, as with prior nelix features. This file drives every seam it CAN with real
components instead of mocking any of them.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from daemon.clock import FakeClock                  # noqa: E402
from daemon.config import ExecutorSpec               # noqa: E402
from daemon.dialog import Dialog                     # noqa: E402
from daemon.drivers.claude import ClaudeDriver       # noqa: E402
from daemon.events import EventQueue                 # noqa: E402
from daemon.hooks import HookEvent                   # noqa: E402
from daemon.hygiene import core_sanitize             # noqa: E402
from daemon.manager import SessionManager            # noqa: E402
from daemon.messages import format_async_reply       # noqa: E402
from daemon.rpc_server import make_server            # noqa: E402
from daemon.session import Session                   # noqa: E402
from daemon.transport import Transport               # noqa: E402
from rpc_client import RpcClient, UnixHTTPConnection  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
QUESTION = ROOT / "bin" / "nelix-question"
NOTE = ROOT / "bin" / "nelix-note"
_FRAME = (Path(__file__).parent / "golden" / "claude" / "idle_prompt" / "bare-prompt.txt").read_text()

_SID = "s1"


class Spec:
    """Mirrors tests/test_async_delivery_realcapture.py::Spec (the fields Session/BeliefEngine read)."""
    driver = "claude"
    settle_seconds = 1.5
    respond_write_seconds = 5.0
    respond_confirm_seconds = 0.3
    delivery_confirm_seconds = 2.0
    max_idle_seconds = 600.0
    startup_timeout_seconds = 60.0
    tail_lines = 100
    status_tail_chars = 4000
    dialog_page_chars = 8000
    spool_max_bytes = 1_000_000

    def argv(self):
        return ["runner", "--interactive"]


class HookFakeHandle:
    """Scripted PTY that stays alive on a single static (REAL-captured) frame — same idiom as
    tests/test_async_delivery_realcapture.py::HookFakeHandle. control_state comes from hooks (the
    production path for the hook-capable claude driver), not this frame's content; writes are
    recorded so a test can assert exactly what (if anything) got typed."""
    def __init__(self, frame, clock=None, step=1.0):
        self._frame = frame
        self.writes = []
        self._clock = clock
        self._step = step

    def pump(self, timeout=0.1):
        if self._clock is not None:
            self._clock.advance(self._step)
        return True

    def render(self):
        return self._frame

    def is_alive(self):
        return True

    def exit_code(self):
        return None

    def write(self, data, timeout=None, drain_output=False):
        self.writes.append(data)

    def finalize(self):
        dialog = getattr(self, "_dialog", None)
        if dialog is not None:
            for ln in self.render().splitlines():
                t = ln.rstrip()
                if t:
                    dialog.add_agent_line(t)

    def leader_pid(self): return 4242
    def leader_pgid(self): return 4242
    def assert_leader_is_group_leader(self): pass

    def leader_status(self):
        from daemon.launchers.base import LeaderStatus
        return LeaderStatus(alive=True, exit_code=None, signal=None, status_available=False)

    def close(self):
        pass


@pytest.fixture
def unix_sock(tmp_path):
    """Short AF_UNIX socket path (<=103 chars incl. NUL) — see tests/test_rpc_server.py."""
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:8]
    p = f"/tmp/nxe{h}.sock"
    yield p
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass


@pytest.fixture
def stack(tmp_path, unix_sock):
    """The REAL daemon stack: EventQueue + SessionManager + Session (scripted PTY, hook-driven),
    registered into the manager exactly as Manager._spawn wires a live session (on_terminal +
    deliver_turn — see daemon/manager.py:168-172), served by a REAL make_server on a REAL unix
    socket in a background thread. Yields (sess, mgr, ev, sid, sock_path)."""
    ev = EventQueue()
    clock = FakeClock(0.0)
    specs = {"demo": ExecutorSpec(command="demo", args=[], env={}, driver="claude")}
    mgr = SessionManager(specs, ev, concurrency_limit=3, session_retain=0, session_max_age_days=0)
    sess = Session(_SID, "demo", ClaudeDriver(), None, Spec(), ev, clock=clock)
    sess._handle = HookFakeHandle(_FRAME, clock=clock)
    sess._dialog = Dialog(tmp_path / _SID, tail_lines=Spec.tail_lines,
                          spool_max_bytes=Spec.spool_max_bytes)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "delivered"
    with mgr._lock:
        mgr._sessions[_SID] = sess
    sess.on_terminal = mgr._free_slot
    sess.deliver_turn = lambda text: mgr.send_turn(_SID, text)

    srv = make_server(mgr, Transport.unix(unix_sock))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield sess, mgr, ev, _SID, unix_sock
    finally:
        srv.shutdown()
        srv.server_close()


def _drive_busy(sess):
    sess.on_hook(HookEvent(_SID, "UserPromptSubmit"))
    sess._loop_once()


def _drive_idle(sess):
    sess.on_hook(HookEvent(_SID, "Stop"))
    sess._loop_once()


def _env(sock, secret, sid=_SID):
    return {"PATH": "/usr/bin:/bin", "NELIX_HOOK_SOCK": sock,
            "NELIX_HOOK_SECRET": secret, "NELIX_SESSION": sid}


def _run(script, args, env, timeout=10):
    return subprocess.run([sys.executable, str(script)] + list(args), env=env,
                          capture_output=True, text=True, timeout=timeout)


def _wait_http(sock, after_seq, session_id, timeout=30):
    """A bare, real HTTP GET /wait long-poll over the real unix socket (no client-side wrapper) —
    exercises daemon.rpc_server's /wait route + EventQueue.wait_event directly."""
    conn = UnixHTTPConnection(sock, timeout=timeout)
    try:
        conn.request("GET", f"/wait?after_seq={after_seq}&session_id={session_id}")
        resp = conn.getresponse()
        body = json.loads(resp.read())
        return resp.status, body
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Ask + wake, note does NOT wake, busy-answer queued + delivered at idle —
#    the full lifecycle narrative through the real stack.
# ---------------------------------------------------------------------------

def test_full_async_message_lifecycle_through_real_daemon(stack):
    sess, mgr, ev, sid, sock = stack
    secret = sess.hook_secret                       # real per-session secret (Session.__init__)
    rpc = RpcClient(Transport.unix(sock))

    _drive_busy(sess)
    before_ask = ev.latest_seq(sid)

    # --- Ask + wake: a REAL background long-poll armed BEFORE the question is posted, so a prompt
    # return proves a genuine notify_all, not a coincidental late poll of an already-published event.
    box = {}
    waiter_started = threading.Event()

    def _waiter():
        waiter_started.set()
        box["result"] = _wait_http(sock, before_ask, sid, timeout=25)

    wt = threading.Thread(target=_waiter, daemon=True)
    t0 = time.monotonic()
    wt.start()
    assert waiter_started.wait(timeout=5), "waiter thread never started"
    time.sleep(0.2)                                  # let the GET actually block inside wait_event

    # The executor posts a question over the REAL unix socket via the REAL wrapper subprocess —
    # never a direct Python call into Session/Manager.
    r = _run(QUESTION, ["--question", "use approach A or B?",
                        "--continuation-plan", "keep implementing A",
                        "--assumption", "A, since it matches the existing pattern"],
             env=_env(sock, secret, sid))
    assert r.returncode == 0, r.stderr
    posted = json.loads(r.stdout.strip())
    assert posted == {"status": "queued", "id": "q_1"}

    wt.join(timeout=10)
    elapsed = time.monotonic() - t0
    assert not wt.is_alive(), "the /wait long-poll never returned"
    assert elapsed < 10.0, f"took {elapsed:.1f}s — looks like it waited out the 25s timeout, not a real notify"
    status, body = box["result"]
    assert status == 200
    evt = body["event"]
    assert evt is not None
    assert evt["kind"] == "async_question"
    assert evt["session_id"] == sid
    assert evt["requires_response"] is True
    assert evt["state"] == "busy"                    # published while the executor was working

    # --- The executor keeps working; async_question is its OWN, non-blocking field.
    snap = rpc.status(sid)
    assert snap["control_state"] == "busy"
    aq = snap["async_question"]
    assert aq["id"] == "q_1"
    assert aq["question"] == "use approach A or B?"
    assert aq["executor_blocked"] is False
    assert "event_id" not in aq                      # internal handle never leaks to the wire

    # --- A note posted mid-flow does NOT advance the event seq and does NOT surface as a wake.
    before_note = ev.latest_seq(sid)
    r = _run(NOTE, ["--summary", "ported half the call sites", "--details", "12 of 24 done"],
             env=_env(sock, secret, sid))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip()) == {"status": "recorded", "progress_seq": 1}
    assert ev.latest_seq(sid) == before_note          # no new event published
    assert ev.wait_event(after_seq=before_note, timeout=0.4, session_id=sid) is None  # never woke
    # The outstanding async_question is ITSELF a wake point (Task 3), so the default snapshot already
    # carries progress here (an orchestrator reading it after the wake sees what happened since) —
    # include_progress=True is the SAME data, not an additional reveal, while a question is pending.
    board = rpc.status(sid)
    assert board["progress"][-1]["summary"] == "ported half the call sites"
    detailed = rpc.status(sid, include_progress=True)
    assert detailed["progress"][-1]["summary"] == "ported half the call sites"
    assert detailed["progress_total"] == 1

    # --- Answer arrives while the executor is STILL BUSY (the common case): accepted + correlated,
    # but delivered NOTHING yet (single-writer PTY — the RPC thread never types).
    ok, resp = rpc.respond(sid, "go with A", decision_id="q_1")
    assert ok is True and resp["status"] == "queued"
    assert resp["next_action"] == "refresh_status"
    assert sess._handle.writes == []                  # nothing typed by the RPC-thread respond
    async_evt = [e for e in ev._events if e.kind == "async_question"][-1]
    assert async_evt.resolved_reason == "answered"     # correlated immediately, regardless of delivery

    # --- The executor finishes its turn; the MONITOR (sole PTY writer) delivers the framed reply as
    # a FRESH turn at the working->idle edge.
    _drive_idle(sess)
    written = "".join(sess._handle.writes)
    reply = format_async_reply("use approach A or B?", "A, since it matches the existing pattern",
                               "go with A")
    # The PTY write path runs the same byte-hygiene every fresh turn does (prepare_pty_input):
    # newlines flatten to spaces before typing (the Session presses the submit key separately) — so
    # the on-the-wire form is core_sanitize(reply), not the raw multi-line block. Comparing against
    # the SAME sanitizer the product uses (not a hand-rolled approximation) keeps this an exact,
    # non-vacuous check of the whole self-contained block landing verbatim (question + assumption +
    # answer + implication), not just a couple of short single-line substrings.
    assert core_sanitize(reply) in written
    assert rpc.status(sid)["control_state"] == "busy"  # a fresh turn re-opened
    assert "async_question" not in rpc.status(sid)


# ---------------------------------------------------------------------------
# 2. Idle-now delivery: the answer lands while the executor is ALREADY idle.
# ---------------------------------------------------------------------------

def test_answer_delivered_immediately_when_executor_already_idle(stack):
    sess, mgr, ev, sid, sock = stack
    secret = sess.hook_secret
    rpc = RpcClient(Transport.unix(sock))

    _drive_busy(sess)
    r = _run(QUESTION, ["--question", "keep the old flag?", "--continuation-plan", "assume yes"],
             env=_env(sock, secret, sid))
    qid = json.loads(r.stdout.strip())["id"]
    _drive_idle(sess)                                  # the executor finishes before the answer arrives
    assert rpc.status(sid)["control_state"] == "idle"
    assert rpc.status(sid)["async_question"]["id"] == qid

    ok, resp = rpc.respond(sid, "yes, keep it", decision_id=qid)
    assert ok is True and resp["status"] == "resumed"  # delivered NOW, not queued
    written = "".join(sess._handle.writes)
    assert "You asked: keep the old flag?" in written
    assert "Hermes: yes, keep it" in written
    assert rpc.status(sid)["control_state"] == "busy"   # re-opened as a fresh turn
    assert "async_question" not in rpc.status(sid)


# ---------------------------------------------------------------------------
# 3. A note never satisfies a genuinely armed waiter (a question does).
# ---------------------------------------------------------------------------

def test_note_does_not_satisfy_a_genuinely_armed_waiter(stack):
    sess, mgr, ev, sid, sock = stack
    secret = sess.hook_secret
    _drive_busy(sess)
    after = ev.latest_seq(sid)

    box = {}

    def _wait_short():
        box["evt"] = ev.wait_event(after_seq=after, timeout=1.0, session_id=sid)

    t0 = time.monotonic()
    wt = threading.Thread(target=_wait_short, daemon=True)
    wt.start()
    time.sleep(0.1)
    r = _run(NOTE, ["--summary", "still working"], env=_env(sock, secret, sid))
    assert r.returncode == 0, r.stderr
    wt.join(timeout=5)
    elapsed = time.monotonic() - t0
    assert box["evt"] is None                          # note never woke the waiter
    assert elapsed >= 0.85                              # genuinely waited out ~the full timeout (some slack for scheduler jitter)

    # The SAME waiter mechanism DOES wake promptly for a question (proves the null result above is
    # because notes don't publish, not because wait_event/notify_all is broken).
    box2 = {}

    def _wait_long():
        box2["evt"] = ev.wait_event(after_seq=after, timeout=25, session_id=sid)

    t1 = time.monotonic()
    wt2 = threading.Thread(target=_wait_long, daemon=True)
    wt2.start()
    time.sleep(0.1)
    r = _run(QUESTION, ["--question", "a?", "--continuation-plan", "c"], env=_env(sock, secret, sid))
    assert r.returncode == 0, r.stderr
    wt2.join(timeout=5)
    assert not wt2.is_alive()
    assert time.monotonic() - t1 < 5.0
    assert box2["evt"] is not None and box2["evt"].kind == "async_question"


# ---------------------------------------------------------------------------
# 4. Terminal survival through the real HTTP layer: the executor exits with the question still
#    outstanding -> a subsequent respond() over real HTTP gets not_delivered, never unknown_session.
# ---------------------------------------------------------------------------

def test_respond_after_executor_exits_returns_not_delivered_through_real_http(stack):
    sess, mgr, ev, sid, sock = stack
    secret = sess.hook_secret
    rpc = RpcClient(Transport.unix(sock))

    _drive_busy(sess)
    r = _run(QUESTION, ["--question", "a?", "--continuation-plan", "c"], env=_env(sock, secret, sid))
    qid = json.loads(r.stdout.strip())["id"]

    sess._stop.set()
    sess._finish()                                      # real terminal funnel -> on_terminal -> _free_slot
    assert mgr.get(sid) is None                          # deregistered

    ok, resp = rpc.respond(sid, "too late", decision_id=qid)
    assert ok is True and resp["status"] == "not_delivered" and resp["reason"] == "executor_finished"
    assert sess._handle.writes == []                     # nothing typed into a dead session


# ---------------------------------------------------------------------------
# 5. already_pending through the real wrapper + real stack (not a FakeManager double).
# ---------------------------------------------------------------------------

def test_second_question_already_pending_through_real_stack(stack):
    sess, mgr, ev, sid, sock = stack
    secret = sess.hook_secret
    _drive_busy(sess)
    r1 = _run(QUESTION, ["--question", "first?", "--continuation-plan", "coding"],
              env=_env(sock, secret, sid))
    assert r1.returncode == 0
    r2 = _run(QUESTION, ["--question", "second?", "--continuation-plan", "coding"],
              env=_env(sock, secret, sid))
    assert r2.returncode != 0
    body = json.loads(r2.stdout.strip())
    assert body["status"] == "already_pending"
    assert body["pending"] == {"id": "q_1", "question": "first?"}
