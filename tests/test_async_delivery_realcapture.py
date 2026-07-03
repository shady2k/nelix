"""Task 4: async answer — resolve + deliver (idle now, queue if busy).

A non-blocking `async_question` (Task 3) leaves the executor UNPAUSED — there is no modal to type an
answer into. When the orchestrator answers, the answer is delivered as a FRESH user turn at the
executor's next idle: NOW if it is already idle, else queued for the MONITOR (the sole PTY writer) to
deliver at the next working->idle transition. Correlation (mark the async-question event answered +
clear the slot) and delivery (the fresh-turn write) are SEPARATE.

These drive a REAL Session over a scripted PTY handle and reach real busy/idle through the loop via
hooks (mirroring tests/test_session_hooks.py — UserPromptSubmit -> busy, Stop -> idle; no hand-set
state), and register that Session into a REAL SessionManager wired exactly as _spawn wires a live one
(on_terminal + deliver_turn), so manager.respond exercises the real id-dispatch + the slot-reacquiring
send_turn write path.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon.clock import FakeClock                # noqa: E402
from daemon.config import ExecutorSpec            # noqa: E402
from daemon.dialog import Dialog                  # noqa: E402
from daemon.drivers.claude import ClaudeDriver    # noqa: E402
from daemon.events import EventQueue              # noqa: E402
from daemon.hooks import HookEvent                # noqa: E402
from daemon.manager import SessionManager         # noqa: E402
from daemon.messages import AsyncQuestion, ProgressNote, format_async_reply  # noqa: E402
from daemon.session import Session                # noqa: E402


class Spec:
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
    """Scripted PTY that stays alive on a single static frame (same idiom as
    tests/test_session_hooks.py). Each pump() advances the injected FakeClock so the engine's
    grace/watchdog windows elapse deterministically. Records every write so a test can assert exactly
    what (if anything) the session typed."""
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


_SID = "s1"


def _manager_session(tmp_path, limit=3, wire_free_slot=True,
                     frame="✦ Working… (esc to interrupt)"):
    """A REAL Session wired to a HookFakeHandle and REGISTERED into a REAL SessionManager exactly as
    _spawn wires a live session (on_terminal + deliver_turn). Returns (sess, mgr, ev)."""
    ev = EventQueue()
    clock = FakeClock(0.0)
    specs = {"demo": ExecutorSpec(command="demo", args=[], env={}, driver="claude")}
    mgr = SessionManager(specs, ev, concurrency_limit=limit,
                         session_retain=0, session_max_age_days=0)
    sess = Session(_SID, "demo", ClaudeDriver(), None, Spec(), ev, clock=clock)
    sess._handle = HookFakeHandle(frame, clock=clock, step=1.0)
    sess._dialog = Dialog(tmp_path / _SID, tail_lines=Spec.tail_lines,
                          spool_max_bytes=Spec.spool_max_bytes)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "delivered"
    sess._clock = clock
    with mgr._lock:
        mgr._sessions[_SID] = sess
    if wire_free_slot:
        sess.on_terminal = mgr._free_slot
    sess.deliver_turn = lambda text: mgr.send_turn(_SID, text)
    return sess, mgr, ev


def _drive_busy(sess):
    sess.on_hook(HookEvent(_SID, "UserPromptSubmit"))
    sess._loop_once()


def _drive_idle(sess):
    sess.on_hook(HookEvent(_SID, "Stop"))
    sess._loop_once()


_Q = AsyncQuestion("a or b?", "keep coding", "a", None)


# ---- format_async_reply (single source of the self-contained reply block) ----

def test_format_reply_is_self_contained():
    text = format_async_reply("a or b?", "a", "use a")
    assert "You asked: a or b?" in text            # restates the question
    assert "Hermes: use a" in text                 # carries the answer
    assert "a" in text                             # the assumption is restated somewhere
    # it is a self-contained block, not a bare answer
    assert text != "use a" and "\n" in text


def test_format_reply_without_assumption():
    text = format_async_reply("a or b?", None, "use a")
    assert "You asked: a or b?" in text and "Hermes: use a" in text


# ---- delivery: idle now / queue if busy / not delivered if terminal ----

def test_answer_delivered_at_idle(tmp_path):
    sess, mgr, _ = _manager_session(tmp_path)
    _drive_busy(sess)
    qid, err = sess.record_async_question(_Q)
    assert err is None
    _drive_idle(sess)                                  # executor finished its turn -> idle
    assert sess.snapshot()["control_state"] == "idle"
    out = mgr.respond(_SID, "use a", decision_id=qid)
    # idle -> delivered NOW as a fresh turn containing the reply block (via slot-reacquiring send_turn)
    assert out.status == "resumed"
    w = "".join(sess._handle.writes)
    assert "You asked: a or b?" in w
    assert "Hermes: use a" in w
    assert not sess.has_pending_async()                # the slot was cleared (correlation)


def test_answer_queued_while_busy_then_delivered(tmp_path):
    sess, mgr, _ = _manager_session(tmp_path)
    _drive_busy(sess)
    qid, _ = sess.record_async_question(_Q)
    out = mgr.respond(_SID, "use a", decision_id=qid)
    assert out.status == "queued"                      # busy -> accepted, not written yet
    assert sess._handle.writes == []                   # busy -> nothing typed
    assert not sess.has_pending_async()                # correlated + slot cleared immediately
    _drive_idle(sess)                                  # monitor drains it at the working->idle edge
    w = "".join(sess._handle.writes)
    assert "You asked: a or b?" in w
    assert "Hermes: use a" in w


def test_terminal_before_answer_is_not_delivered(tmp_path):
    # In-Session terminal guard: the session went terminal before the answer arrived -> not_delivered,
    # NOTHING typed. (The MANAGER-freed-session sub-case — respond after the slot is freed returning
    # not_delivered/executor_finished instead of unknown_session — is Task 6; here the terminal session
    # is still registered so the id-dispatch reaches resolve_async_question.)
    sess, mgr, _ = _manager_session(tmp_path, wire_free_slot=False)
    _drive_busy(sess)
    qid, _ = sess.record_async_question(_Q)
    sess._stop.set()
    sess._finish()                                     # real terminal funnel (stopped)
    assert sess.snapshot()["control_state"] == "terminal"
    out = mgr.respond(_SID, "use a", decision_id=qid)
    assert out.status == "not_delivered"
    assert sess._handle.writes == []                   # nothing typed into a dead session


def test_respond_after_finish_returns_not_delivered(tmp_path):
    # The MANAGER-freed-session sub-case (Task 6): the executor exits WHILE its async question is
    # still outstanding. Terminal cleanup (on_terminal -> Manager._free_slot, wire_free_slot=True is
    # the default here) auto-resolves the question (executor_finished) into a manager-level
    # recent-terminal async store (same key/expiry as self._terminal). A subsequent respond() naming
    # that decision_id must get a clean not_delivered/executor_finished, never a bare unknown_session
    # (which would look like a typo'd/unrelated session id to the orchestrator).
    sess, mgr, _ = _manager_session(tmp_path)             # wire_free_slot=True (default)
    _drive_busy(sess)
    qid, _ = sess.record_async_question(_Q)
    sess._stop.set()
    sess._finish()                                        # real terminal funnel -> _free_slot runs
    assert mgr.get(_SID) is None                          # deregistered: the slot was freed
    out = mgr.respond(_SID, "use a", decision_id=qid)
    assert out.status == "not_delivered" and out.reason == "executor_finished"
    assert sess._handle.writes == []                      # nothing typed — the executor is gone


def test_terminal_snapshot_carries_progress_trail(tmp_path):
    # I1 (final whole-branch review): _finish publishes the terminal event then synchronously frees
    # the slot (Manager._free_slot), so by the time Hermes reads status the session lives only in
    # recent_terminal / status()'s terminal_snapshot() path. Progress notes recorded before exit must
    # still be visible there — otherwise the curated progress trail (spec §7/§1: what the executor
    # accomplished) is lost exactly at completion, the one moment it matters most.
    sess, mgr, _ = _manager_session(tmp_path)
    _drive_busy(sess)
    sess.append_progress_note(ProgressNote("did step 1", None))
    sess.append_progress_note(ProgressNote("did step 2", "more detail"))
    sess._stop.set()
    sess._finish()                                        # real terminal funnel -> _free_slot runs
    assert mgr.get(_SID) is None                          # deregistered: the slot was freed
    board = mgr.status()
    snap = board["recent_terminal"][_SID]
    assert snap["progress_total"] == 2
    summaries = [n["summary"] for n in snap["progress"]]
    assert summaries == ["did step 1", "did step 2"]


def test_respond_after_finish_wrong_id_is_unknown_session(tmp_path):
    # The terminal-survival fallback is narrowly scoped to the id that was actually auto-resolved;
    # any other decision_id for the same freed session still falls through to unknown_session.
    sess, mgr, _ = _manager_session(tmp_path)
    _drive_busy(sess)
    sess.record_async_question(_Q)
    sess._stop.set()
    sess._finish()
    out = mgr.respond(_SID, "use a", decision_id="q_999")
    assert out.status == "unknown_session"


# ---- resolution / dispatch invariants ----

def test_resolve_marks_the_async_event_answered(tmp_path):
    sess, mgr, ev = _manager_session(tmp_path)
    _drive_busy(sess)
    qid, _ = sess.record_async_question(_Q)
    async_evt = [e for e in ev._events if e.kind == "async_question"][-1]
    assert async_evt.resolved_reason is None
    mgr.respond(_SID, "use a", decision_id=qid)
    assert async_evt.resolved_reason == "answered"     # correlated by decision_id -> mark_answered


def test_resolve_async_question_busy_does_not_write(tmp_path):
    # The RPC-thread resolve NEVER writes the PTY when busy — it only enqueues; the monitor writes.
    sess, _, _ = _manager_session(tmp_path)
    _drive_busy(sess)
    qid, _ = sess.record_async_question(_Q)
    disposition, text = sess.resolve_async_question(qid, "use a")
    assert disposition == "queued_busy"
    assert sess._async_reply_pending == text           # enqueued for the monitor
    assert sess._handle.writes == []                   # RPC thread typed nothing


def test_wrong_id_does_not_resolve(tmp_path):
    sess, _, _ = _manager_session(tmp_path)
    _drive_busy(sess)
    sess.record_async_question(_Q)
    disposition, text = sess.resolve_async_question("q_999", "use a")
    assert disposition == "not_delivered" and text is None
    assert sess.has_pending_async()                    # the real question is untouched


class _BusyStub:
    """Minimal manager-facing session double that occupies an ACTIVE slot (control_state=busy), so a
    test can saturate the concurrency limit and force an idle resume to be refused at_capacity."""
    def snapshot(self):
        return {"control_state": "busy"}


def test_idle_now_at_capacity_requeues_full_frame_not_lost(tmp_path):
    # Important-fix regression: an async question answered while the session is idle-now but the
    # concurrency limit is saturated -> send_turn refuses at_capacity. The slot was already cleared +
    # the event marked answered, so the FRAMED reply must be re-queued (not lost, not later delivered
    # as a bare answer) and delivered with the full frame once capacity frees at the next idle.
    sess, mgr, _ = _manager_session(tmp_path, limit=1)
    _drive_busy(sess)
    qid, _ = sess.record_async_question(_Q)
    _drive_idle(sess)                                    # sess idle (freed its own active slot)
    assert sess.snapshot()["control_state"] == "idle"
    with mgr._lock:
        mgr._sessions["other"] = _BusyStub()            # a second busy session saturates limit=1
    out = mgr.respond(_SID, "use a", decision_id=qid)
    assert out.status == "at_capacity"
    assert sess._handle.writes == []                     # nothing typed on the refusal
    assert sess._async_reply_pending is not None          # re-queued, NOT lost
    assert "You asked: a or b?" in sess._async_reply_pending   # the FULL frame, not a bare answer
    assert "Hermes: use a" in sess._async_reply_pending
    with mgr._lock:
        del mgr._sessions["other"]                       # capacity frees
    sess._loop_once()                                    # still idle -> monitor drain retries -> delivers
    w = "".join(sess._handle.writes)
    assert "You asked: a or b?" in w and "Hermes: use a" in w
    assert sess._async_reply_pending is None


def test_busy_reply_requeued_when_delivery_declines(tmp_path):
    # If send_turn declines at the idle edge WITHOUT typing (no free slot now, or a concurrent idle
    # follow-up already resumed the session), the queued reply is retried at the next idle — never
    # silently dropped. A partial write / terminal outcome is deliberately NOT retried.
    from daemon.session import RespondOutcome
    sess, _, _ = _manager_session(tmp_path)
    _drive_busy(sess)
    qid, _ = sess.record_async_question(_Q)
    sess.resolve_async_question(qid, "use a")            # busy -> enqueued
    assert sess._async_reply_pending is not None
    calls = []

    def flaky(text):
        calls.append(text)
        return RespondOutcome("at_capacity") if len(calls) == 1 else RespondOutcome("resumed")

    sess.deliver_turn = flaky
    _drive_idle(sess)                                    # idle: drain fires -> at_capacity -> re-queued
    assert sess._async_reply_pending is not None         # not dropped
    sess.drain_async_reply()                             # next idle tick -> delivered
    assert sess._async_reply_pending is None
    assert len(calls) == 2 and "Hermes: use a" in calls[-1]


def test_second_question_rejected_while_earlier_reply_still_queued(tmp_path):
    # M2 (final whole-branch review): the already_pending guard in record_async_question checks
    # only self._async_question, which is cleared the moment an answer is CORRELATED (even though
    # the reply itself is only QUEUED, not yet delivered, while busy). Without this guard: busy ->
    # ask q1 -> answer q1 (slot cleared, reply1 queued) -> still busy -> ask q2 (wrongly accepted,
    # since the slot was cleared) -> answer q2 -> reply2 OVERWRITES reply1 in the single
    # _async_reply_pending slot -> reply1 is silently lost even though Hermes was told "queued".
    sess, mgr, _ = _manager_session(tmp_path)
    _drive_busy(sess)
    qid1, err1 = sess.record_async_question(_Q)
    assert err1 is None
    out1 = mgr.respond(_SID, "use a", decision_id=qid1)
    assert out1.status == "queued"                      # busy -> reply1 enqueued, not delivered yet
    assert not sess.has_pending_async()                  # q1's slot was cleared (correlation)
    reply1 = sess._async_reply_pending
    assert reply1 is not None and "Hermes: use a" in reply1

    # Still busy: a second question must be REJECTED (already_pending shape), not accepted — else
    # answering it would clobber reply1.
    q2 = AsyncQuestion("c or d?", "keep coding", "c", None)
    qid2, err2 = sess.record_async_question(q2)
    assert qid2 is None
    assert err2 is not None and "id" in err2             # same already_pending shape as the
                                                          # live-question branch (route maps -> 409)
    assert sess._async_reply_pending == reply1            # reply1 is UNTOUCHED, still queued

    # Once the executor goes idle, reply1 (and only reply1) is delivered — nothing was lost.
    _drive_idle(sess)
    w = "".join(sess._handle.writes)
    assert "Hermes: use a" in w
    assert "c or d?" not in w                             # q2 was never accepted, never answered


def test_plain_idle_followup_still_routes_to_send_turn(tmp_path):
    # Regression: an idle follow-up with NO async question outstanding still resumes via send_turn
    # (the existing idle routing must stay untouched — the async dispatch only fires on a pending id).
    sess, mgr, _ = _manager_session(tmp_path)
    _drive_busy(sess)
    _drive_idle(sess)
    out = mgr.respond(_SID, "keep going")
    assert out.status == "resumed"
    assert "keep going" in "".join(sess._handle.writes)
