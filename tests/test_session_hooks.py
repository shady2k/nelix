"""Task 8: Session hook plumbing — on_hook queue, _loop drain, hook_secret, idle publish, keep-alive.

These drive the post-delivery run loop directly via a single-iteration seam `_loop_once()` (mirroring
how tests/test_session.py drives `_loop`). The belief engine's hook path (Task 6/7) is the source of
truth; here we verify Session's plumbing: a Stop hook publishes the non-respondable `idle` decision and
the session STAYS ALIVE (never types `exit`), a structured ask publishes a respondable
`waiting_for_user`, and while hooks are active the screen NEVER publishes a screen-derived decision.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.session import Session               # noqa: E402
from daemon.dialog import Dialog                 # noqa: E402
from daemon.drivers.claude import ClaudeDriver   # noqa: E402
from daemon.events import EventQueue             # noqa: E402
from daemon.hooks import HookEvent               # noqa: E402
from daemon.clock import FakeClock               # noqa: E402
from daemon.obs import Logger                     # noqa: E402
import io                                          # noqa: E402


class Spec:
    driver = "claude"
    settle_seconds = 1.5
    respond_write_seconds = 5.0
    respond_confirm_seconds = 0.3
    delivery_confirm_seconds = 2.0
    max_idle_seconds = 600.0
    tail_lines = 100
    status_tail_chars = 4000
    dialog_page_chars = 8000
    spool_max_bytes = 1_000_000

    def argv(self):
        return ["runner", "--interactive"]


class HookFakeHandle:
    """Scripted PTY that stays alive on a single static frame. Each pump() advances the injected
    FakeClock by `step` so the engine's grace/watchdog windows elapse deterministically (no sleeps).
    Records every write so a test can assert the session typed NOTHING (never an auto-`exit`)."""
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


def _hook_session(tmp_path, frame="✦ Working… (esc to interrupt)", step=1.0, logger=None):
    ev = EventQueue()
    clock = FakeClock(0.0)
    sess = Session("s1", "demo", ClaudeDriver(), None, Spec(), ev, logger=logger, clock=clock)
    sess._handle = HookFakeHandle(frame, clock=clock, step=step)
    sess._dialog = Dialog(tmp_path / "s1", tail_lines=Spec.tail_lines,
                          spool_max_bytes=Spec.spool_max_bytes)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "delivered"
    sess._clock = clock
    return sess, ev


def test_drain_hooks_with_real_logger_does_not_crash(tmp_path):
    # Regression (s-6a4e8c61): the production monitor thread runs with a real Logger, and _drain_hooks
    # logs a `hook_applied` line per drained hook. Every other hook test wires logger=None, so that
    # log call — the one line that fires on the FIRST hook in prod — was dead code under test. A
    # positional/keyword `event=` collision (session.py) made Logger.debug() raise TypeError, killing
    # the monitor on the first hook. Drive the drain path with a real Logger so the call is exercised.
    buf = io.StringIO()
    log = Logger(level="debug", stream=buf)
    sess, _ = _hook_session(tmp_path, logger=log)
    sess.on_hook(HookEvent("s1", "UserPromptSubmit"))
    sess._loop_once()                                  # drains the hook → _drain_hooks logs hook_applied
    assert '"event": "hook_applied"' in buf.getvalue()  # the log line was actually emitted, no crash


def test_hook_secret_generated_per_session(tmp_path):
    a, _ = _hook_session(tmp_path)
    b, _ = _hook_session(tmp_path)
    assert isinstance(a.hook_secret, str) and len(a.hook_secret) == 32   # token_hex(16)
    assert a.hook_secret != b.hook_secret                               # per-session, not shared


def test_on_hook_only_enqueues_never_processes(tmp_path):
    # on_hook is called on the RPC thread: it must ONLY enqueue (never block, never touch the engine).
    sess, ev = _hook_session(tmp_path)
    sess.on_hook(HookEvent("s1", "Stop"))
    assert sess._engine.hook_mode == "unknown"       # engine untouched until the loop drains
    assert sess._decision is None
    assert not ev._events                            # nothing published from on_hook itself


def test_stop_hook_publishes_idle_and_keeps_session_alive(tmp_path):
    sess, ev = _hook_session(tmp_path)
    sess.on_hook(HookEvent("s1", "UserPromptSubmit"))
    sess.on_hook(HookEvent("s1", "Stop"))
    sess._loop_once()                                 # drains BOTH queued hooks
    snap = sess.snapshot()
    assert snap["control_state"] == "idle"
    assert snap["decision"]["kind"] == "idle"
    assert snap["decision"]["requires_response"] is False
    assert snap["pending"] is False                   # idle is non-respondable
    assert not sess._closing                          # the session STAYS ALIVE
    idle_evs = [e for e in ev._events if e.kind == "idle"]
    assert idle_evs and idle_evs[-1].requires_response is False   # a wake event, but non-respondable
    assert ev.pending("s1") is None                   # never in the respondable queue


def test_idle_does_not_type_exit(tmp_path):
    # Regression (the auto-`exit` bug): reaching idle must NEVER write to the PTY.
    sess, ev = _hook_session(tmp_path)
    sess.on_hook(HookEvent("s1", "UserPromptSubmit"))
    sess.on_hook(HookEvent("s1", "Stop"))
    sess._loop_once()
    assert sess._handle.writes == []                  # nothing typed at all — no exit/quit
    assert sess.snapshot()["decision"]["kind"] == "idle"


def test_askquestion_hook_publishes_waiting(tmp_path):
    sess, ev = _hook_session(tmp_path)
    sess.on_hook(HookEvent("s1", "UserPromptSubmit"))
    sess.on_hook(HookEvent("s1", "PreToolUse", tool_name="AskUserQuestion",
                           tool_input={"question": "JSON or YAML?"}))
    sess._loop_once()
    snap = sess.snapshot()
    assert snap["control_state"] == "awaiting_user"
    dec = snap["decision"]
    assert dec["kind"] == "waiting_for_user" and dec["requires_response"] is True
    assert dec["prompt_kind"] == "modal_choice"
    assert ev.pending("s1") is not None               # respondable
    assert sess._handle.writes == []                  # a pause never types anything


def _drive_to_idle(sess):
    sess.on_hook(HookEvent("s1", "UserPromptSubmit"))
    sess.on_hook(HookEvent("s1", "Stop"))
    sess._loop_once()


def test_send_turn_on_idle_types_submission_and_resumes(tmp_path):
    # Task 10: a follow-up on an idle session re-opens the turn — type the framed submission + the
    # submit key (nothing else, never `exit`), return resumed, and leave control_state active again.
    sess, ev = _hook_session(tmp_path)
    _drive_to_idle(sess)
    assert sess.snapshot()["control_state"] == "idle"
    out = sess.send_turn("please continue")
    assert out.status == "resumed"
    # bracketed-paste framed text, THEN CR — the exact pair _deliver_task types, and NOTHING else.
    assert sess._handle.writes == ["\x1b[200~please continue\x1b[201~", "\r"]
    snap = sess.snapshot()
    assert snap["control_state"] == "busy"            # re-acquired an active slot (turn resuming)
    assert snap["pending"] is False                   # did NOT fabricate a pending decision
    assert "decision" not in snap                      # the non-respondable idle decision was dropped


def test_send_turn_rejected_when_not_idle(tmp_path):
    # send_turn is allowed ONLY from idle: a busy session must reject it and type nothing.
    sess, ev = _hook_session(tmp_path)
    sess.on_hook(HookEvent("s1", "UserPromptSubmit"))
    sess._loop_once()                                 # busy (no Stop)
    assert sess.snapshot()["control_state"] == "busy"
    out = sess.send_turn("too soon")
    assert out.status != "resumed"
    assert sess._handle.writes == []                  # nothing typed when not idle


def test_send_turn_appends_followup_to_transcript(tmp_path):
    # The follow-up is a new user turn -> it must land in the transcript (like the initial task).
    sess, ev = _hook_session(tmp_path)
    _drive_to_idle(sess)
    sess.send_turn("do the next thing")
    page = sess._dialog.page(0)
    assert "do the next thing" in page["text"]


def test_first_hook_withdraws_stale_screen_waiting(tmp_path):
    # CRITICAL 1: the screen fallback published a respondable waiting_for_user BEFORE any hook. The
    # first hook (UserPromptSubmit) takes over -> the stale screen decision must be WITHDRAWN (gone
    # from the snapshot) and the session goes busy. A lingering waiting_for_user must not survive.
    perm_box = "Proceed with the edit?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No\n"
    sess, ev = _hook_session(tmp_path, frame=perm_box)
    sess._loop_once()                                        # screen publishes waiting_for_user (no hooks)
    assert sess._engine.hook_mode == "unknown"
    assert sess.snapshot()["decision"]["kind"] == "waiting_for_user"
    assert sess.snapshot()["pending"] is True
    sess.on_hook(HookEvent("s1", "UserPromptSubmit"))        # first hook: hooks take over
    sess._loop_once()
    snap = sess.snapshot()
    assert sess._engine.hook_mode == "active"
    assert snap["control_state"] == "busy"
    assert snap["pending"] is False
    assert "decision" not in snap                            # the stale screen decision was withdrawn
    assert ev.pending("s1") is None                          # no longer in the respondable queue


def test_hook_mode_active_suppresses_screen_decision(tmp_path):
    # A real permission menu WOULD publish a screen-derived waiting_for_user in screen mode. While
    # hooks are active (ground truth), the screen must NOT publish it — hooks own the state.
    perm_box = "Proceed with the edit?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No\n"
    sess, ev = _hook_session(tmp_path, frame=perm_box)
    sess.on_hook(HookEvent("s1", "UserPromptSubmit"))     # hook_active + busy
    for _ in range(6):
        sess._loop_once()
    assert sess._engine.hook_mode == "active"
    assert not any(e.kind == "waiting_for_user" for e in ev._events)   # screen decision suppressed
    assert ev.pending("s1") is None
    assert sess.snapshot()["control_state"] == "busy"                   # hooks own the state
    assert sess._handle.writes == []
