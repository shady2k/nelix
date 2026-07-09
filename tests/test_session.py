import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths                                   # noqa: E402
from daemon.session import Session            # noqa: E402
from daemon.dialog import Dialog              # noqa: E402
from daemon.drivers.claude import ClaudeDriver  # noqa: E402
from daemon.events import EventQueue          # noqa: E402
from daemon.hooks import HookEvent            # noqa: E402
from tests._session_replay import default_logger, _REAL_LOGGER  # noqa: E402


class Spec:
    driver = "claude"
    settle_seconds = 1.5
    respond_write_seconds = 5.0
    respond_confirm_seconds = 0.3       # short submit-confirm window for fast tests
    delivery_confirm_seconds = 2.0
    max_idle_seconds = 600.0
    startup_timeout_seconds = 60.0
    tail_lines = 100
    status_tail_chars = 4000
    spool_max_bytes = 1_000_000

    def argv(self):
        return ["runner", "--interactive"]   # fictional; mirrors ExecutorSpec.argv()


class HangSpec(Spec):
    max_idle_seconds = 5.0


class BackstopSpec(Spec):
    max_idle_seconds = 0.2          # fast no-progress backstop for real-thread tests


class FastConfirmSpec(Spec):
    delivery_confirm_seconds = 0.3      # fast timeout so the failure path is quick to test


class TruncSpec(Spec):
    status_tail_chars = 5


class FakeHandle:
    """Scripted PTY: render() walks `frames`; process stays alive, the loop is terminated
    by setting `stop` once the last frame is reached (so observe never sees a false exit).
    Each pump() advances the injected FakeClock by `step` so the engine's settle/grace windows
    elapse deterministically (no real sleeps, no time.* in the belief path)."""
    def __init__(self, frames, stop=None, clock=None, step=1.0):
        self.frames = frames
        self.i = -1
        self.writes = []
        self._stop = stop
        self._clock = clock
        self._step = step

    def pump(self, timeout=0.1):
        self.i += 1
        if self._clock is not None:
            self._clock.advance(self._step)
        if self._stop is not None and self.i >= len(self.frames) - 1:
            self._stop.set()
        return True

    def render(self):
        return self.frames[min(self.i, len(self.frames) - 1)]

    def is_alive(self):
        return True

    def exit_code(self):
        return None

    def write(self, data, timeout=None, drain_output=False):
        self.writes.append(data)

    def finalize(self):
        dialog = getattr(self, '_dialog', None)
        if dialog is not None:
            for ln in self.render().splitlines():
                t = ln.rstrip()
                if t:
                    dialog.add_agent_line(t)

    def leader_pid(self): return 4242
    def leader_pgid(self): return 4242
    def assert_leader_is_group_leader(self):
        pid, pgid = self.leader_pid(), self.leader_pgid()
        if pid is None or pid != pgid:
            raise RuntimeError(f"pty leader {pid} is not its own group leader (pgid={pgid})")
    def leader_status(self):
        from daemon.launchers.base import LeaderStatus
        if self.is_alive():
            return LeaderStatus(alive=True, exit_code=None, signal=None, status_available=False)
        return LeaderStatus(alive=False, exit_code=0, signal=None, status_available=True)

    def close(self):
        pass


class DeadHandle:
    """Child already exited with `code`."""
    def __init__(self, code, frame="bye"):
        self._code = code
        self._frame = frame
        self.writes = []

    def pump(self, timeout=0.1):
        return False

    def render(self):
        return self._frame

    def is_alive(self):
        return False

    def exit_code(self):
        return self._code

    def write(self, data, timeout=None, drain_output=False):
        self.writes.append(data)

    def finalize(self):
        pass

    def leader_pid(self): return 4242
    def leader_pgid(self): return 4242
    def assert_leader_is_group_leader(self):
        pid, pgid = self.leader_pid(), self.leader_pgid()
        if pid is None or pid != pgid:
            raise RuntimeError(f"pty leader {pid} is not its own group leader (pgid={pgid})")
    def leader_status(self):
        from daemon.launchers.base import LeaderStatus
        return LeaderStatus(alive=False, exit_code=self._code, signal=None,
                            status_available=True)

    def close(self):
        pass


class _NoopLauncher:
    def __init__(self, handle): self._handle = handle
    def start(self, spec, cwd, cols, rows, dialog=None, transcript=None, **_kw): return self._handle
    def stop(self, handle): handle.close()


def _clock(values):
    it = iter(values)
    last = [0.0]

    def now():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]
    return now


def _session(tmp_path, frames=(), handle=None, spec=None, step=1.0, logger=_REAL_LOGGER):
    from daemon.clock import FakeClock
    if logger is _REAL_LOGGER:
        logger = default_logger()          # real Logger by default -> every self._log.* site fires
    ev = EventQueue()
    clock = FakeClock(0.0)
    sess = Session("s1", "demo", ClaudeDriver(), None, spec or Spec(), ev, clock=clock, logger=logger)
    sess._handle = (handle if handle is not None
                    else FakeHandle(list(frames), stop=sess._stop, clock=clock, step=step))
    sess._dialog = Dialog(tmp_path / "s1", tail_lines=Spec.tail_lines,
                          spool_max_bytes=Spec.spool_max_bytes)
    sess._handle._dialog = sess._dialog   # give fake handle a dialog ref for finalize()
    sess._task_delivery = "delivered"     # these tests drive the post-delivery run loop directly
    sess._clock = clock
    return sess, ev


# nelix-s7v Approach B: the post-delivery free-text submit is written AND confirmed by the MONITOR
# (_drain_pending_submit), so respond() no longer completes synchronously on the calling thread — it
# enqueues _pending_submit and blocks until the monitor resolves it. These frame constants + harness
# drive that monitor role deterministically. The FIRST frame the drain observes must be the free_text
# box (so the monitor writes); later frames are the post-write confirm evidence (echo / moved).
_BOX = "Ready — what next?\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"   # free_text prompt, empty input box
_WORKING = "✶ Working… (esc to interrupt)"                          # the turn MOVED (none + heartbeat)
_UNKNOWN = "❯ "                                                      # ambiguous: box marker, no footer


def _echo(answer):
    """The free_text box with `answer` echoed into the input line (submitted_echo_present)."""
    return f"Ready — what next?\n❯ {answer}\n⏵⏵ ask mode (shift+tab to cycle)"


def respond_via_submit_monitor(sess, answer, decision_id, frames, *, timeout=10.0):
    """Approach B two-thread model of a POST-delivery free-text respond(): respond() runs on a worker
    (RPC) thread and ENQUEUES _pending_submit; THIS thread is the monitor — it drains the submit against
    each scripted frame in turn (observe -> _drain_pending_submit) until the submit resolves, then joins
    the worker and returns its RespondOutcome. `frames` is the post-enqueue progression the monitor
    'pumps': the FIRST compatible free_text frame drives the write; later frames drive the confirm; the
    last frame repeats while the confirm window elapses (the stranded/never-echo case). The PTY write
    goes to sess._handle (records it); nothing here calls _handle.render()."""
    box = {}

    def rpc():
        try:
            box["out"] = sess.respond(answer, decision_id=decision_id)
        except BaseException as exc:        # pragma: no cover - surfaced via the assert below
            box["err"] = exc
    t = threading.Thread(target=rpc, daemon=True)
    t.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:      # wait for respond() to enqueue the submit
        with sess._lock:
            enqueued = sess._pending_submit is not None
        if enqueued:
            break
        time.sleep(0)
    else:
        t.join(timeout=1.0)
        raise AssertionError(box.get("err") or "respond() never enqueued a pending submit")
    seq = list(frames)
    i = 0
    while time.monotonic() < deadline:
        with sess._lock:
            pending = sess._pending_submit is not None
        if not pending:
            break
        frame = seq[min(i, len(seq) - 1)]
        obs = sess._driver.observe(frame, sess._obs_ctx())
        sess._drain_pending_submit(obs)
        i += 1
        time.sleep(0.003)                   # let the confirm window elapse in real time when stranded
    t.join(timeout=timeout)
    if "err" in box:
        raise box["err"]
    return box["out"]


def respond_driving_loop(sess, answer, decision_id, *, settle=0.03, timeout=8.0):
    """Version-AGNOSTIC harness (works on the 7850b28 render-gen confirm AND Approach B): respond() on a
    worker thread + the REAL _loop_once driven on THIS (monitor) thread over sess._handle, until
    respond() returns. On 7850b28 the loop caches _last_render / bumps _render_gen and respond() reads
    them; on Approach B the loop drains _pending_submit. A short settle lets respond() get in-flight
    (claim / enqueue) before the loop's engine tick runs, so the tick never races the claim. Used for
    the FALSE-CONFIRM AC, whose RED must be demonstrable on 7850b28."""
    box = {}

    def rpc():
        try:
            box["out"] = sess.respond(answer, decision_id=decision_id)
        except BaseException as exc:        # pragma: no cover
            box["err"] = exc
    t = threading.Thread(target=rpc, daemon=True)
    t.start()
    time.sleep(settle)
    deadline = time.monotonic() + timeout
    while t.is_alive() and time.monotonic() < deadline:
        try:
            sess._loop_once()
        except Exception:                   # pragma: no cover - a torn-down handle ends the drive
            break
        time.sleep(0.005)
    t.join(timeout=2.0)
    if "err" in box:
        raise box["err"]
    return box["out"]


def test_sessions_dir_resolves_under_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sess = Session("s1", "demo", ClaudeDriver(), None, Spec(), EventQueue())
    assert sess._sessions_dir == paths.sessions_root()


def test_loop_publishes_one_waiting_for_user_on_stable_idle(tmp_path):
    # Task 8: the monitor drives observe() -> BeliefEngine -> actions. A working frame publishes
    # nothing; a stable idle prompt publishes exactly one waiting_for_user. Clock is injected.
    box = "Here is my answer.\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"
    sess, ev = _session(tmp_path, ["thinking… esc to interrupt", box, box, box])
    sess._loop()
    pubs = [e for e in ev._events if e.kind == "waiting_for_user"]
    assert len(pubs) == 1                                 # exactly one decision
    assert sess._state == "awaiting_user"
    assert sess._decision is not None and sess._decision["kind"] == "waiting_for_user"


def test_loop_working_frame_publishes_nothing(tmp_path):
    sess, ev = _session(tmp_path, ["compiling things… esc to interrupt",
                                   "compiling things… esc to interrupt"])
    sess._loop()
    assert ev.pending("s1") is None
    assert sess._state == "busy" and sess.is_working() is True


def test_status_exposes_rich_belief_fields(tmp_path):
    # Task 14: /status (snapshot) exposes control_state + busy_reason + liveness + quiet_elapsed +
    # escalation_count — the same fields feed the live status, the trail, and the replay oracle.
    box = "Here is my answer.\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"
    sess, ev = _session(tmp_path, ["thinking… esc to interrupt", box, box, box])
    sess._loop()
    snap = sess.snapshot()
    assert snap["control_state"] == "awaiting_user"
    for k in ("busy_reason", "liveness", "quiet_elapsed", "escalation_count"):
        assert k in snap
    assert "state" not in snap                          # the old per-driver state string is gone


def test_terminal_snapshot_shape(tmp_path):
    sess, ev = _session(tmp_path, handle=DeadHandle(0))
    sess._spawn_ts = 0.0
    sess._run()
    snap = sess.snapshot()
    assert snap["control_state"] == "terminal"          # not a per-driver state string
    assert snap["terminal_kind"] == "done"
    assert snap["pending"] is False and "state" not in snap
    ts = sess.terminal_snapshot()
    assert ts["control_state"] == "terminal" and ts["terminal_kind"] == "done"
    assert ts["pending"] is False and "state" not in ts


def test_belief_transition_trail_is_logged(tmp_path):
    # Every engine action writes a trail line carrying the observation that drove it + fingerprints +
    # the rule that fired (spec §8). This is the diagnostic line AND the replay oracle.
    import io, json
    from daemon.obs import Logger
    buf = io.StringIO()
    log = Logger(level="debug", stream=buf, audit_stream=buf)
    box = "Here is my answer.\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"
    sess, ev = _session(tmp_path, ["thinking… esc to interrupt", box, box, box])
    sess._log = log
    sess._loop()
    trail = [json.loads(l) for l in buf.getvalue().splitlines()
             if json.loads(l)["event"] == "belief_transition"]
    assert trail, "expected at least one belief_transition trail line"
    pub = [t for t in trail if t["rule"] == "publish:waiting_for_user"]
    assert pub, "the published decision must be in the trail"
    line = pub[0]
    for k in ("prompt_kind", "affordances", "busy_reason", "liveness",
              "semantic_fp", "content_fp", "prompt_fp", "rule"):
        assert k in line
    assert line["prompt_kind"] == "free_text"


def test_stop_edge_emits_frozen_respondable_event(tmp_path):
    frames = ["thinking… esc to interrupt", "Here is my answer. Which next?\n❯ \n⏵⏵ ask mode (shift+tab to cycle)",
              "Here is my answer. Which next?\n❯ \n⏵⏵ ask mode (shift+tab to cycle)", "Here is my answer. Which next?\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"]
    sess, ev = _session(tmp_path, frames)
    sess._loop()
    snap = sess.snapshot()
    assert snap["control_state"] == "awaiting_user"
    dec = snap["decision"]
    assert dec["kind"] == "waiting_for_user"
    assert "Here is my answer." in dec["text"]
    pend = ev.pending("s1")
    assert pend is not None and pend.event_id == dec["event_id"]
    # After emit, later output must NOT change the event's frozen tail text.
    frozen = dec["text"]
    sess._dialog.add_agent_line("LATE OUTPUT")
    assert sess.snapshot()["decision"]["text"] == frozen
    assert "LATE OUTPUT" not in sess.snapshot()["decision"]["text"]


def test_decision_reports_truncation(tmp_path):
    box = "Hello, what now?\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"
    sess, _ = _session(tmp_path, ["working esc to interrupt", box, box, box], spec=TruncSpec())
    sess._loop()
    dec = sess.snapshot()["decision"]
    assert dec["truncated"] is True
    assert dec["total_len"] > len(dec["text"]) and len(dec["text"]) <= 5


def test_quiet_working_emits_no_event(tmp_path):
    sess, ev = _session(tmp_path, ["compiling…", "compiling…"])
    sess._loop()
    assert ev.pending("s1") is None
    assert sess.snapshot()["control_state"] == "busy"


def test_permission_prompt_carries_needs_permission_hint(tmp_path):
    # A real claude permission menu (cursor on "1. Yes", a "3. No" option) surfaces as a
    # permission_choice carrying the needs_permission hint and its options (fixes F2 routing).
    box = "Proceed with the edit?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No\n"
    sess, ev = _session(tmp_path, ["working esc to interrupt", box, box, box])
    sess._loop()
    dec = sess.snapshot()["decision"]
    assert dec["kind"] == "waiting_for_user" and dec["hint"] == "needs_permission"
    assert dec["prompt_kind"] == "permission_choice"
    assert [o["id"] for o in dec["options"]] == ["1", "2", "3"]
    assert ev.pending("s1").hint == "needs_permission"


def test_exit_zero_emits_done(monkeypatch, tmp_path):
    sess, ev = _session(tmp_path, handle=DeadHandle(0))
    sess._spawn_ts = 0.0
    sess._run()                                           # finally -> _finish publishes terminal
    assert ev.pending("s1") is None                       # 'done' is not respondable
    last = ev.latest_after(0)
    assert last is not None and last.kind == "done"
    assert sess.snapshot()["control_state"] == "terminal"  # was state="exited"


def test_exit_nonzero_emits_crashed(monkeypatch, tmp_path):
    sess, ev = _session(tmp_path, handle=DeadHandle(2))
    sess._spawn_ts = 0.0
    sess._run()
    last = ev.latest_after(0)
    assert last is not None and last.kind == "crashed"
    assert sess.snapshot()["control_state"] == "terminal"  # was state="crashed"


def test_loop_never_writes_esc(tmp_path):
    # The daemon is a passive bridge (P2): the monitor loop NEVER writes ESC (or any byte) to the
    # agent. A long-running working screen escalates an advisory (Task 13), it does not nudge.
    sess, ev = _session(tmp_path, ["working… esc to interrupt"] * 3, spec=HangSpec())
    sess._loop()
    assert "\x1b" not in sess._handle.writes              # no ESC nudge / no action on the agent
    assert sess._handle.writes == []                      # nothing typed at all


class InterventionSpec(Spec):
    heartbeat_stale_after = 1.0
    stale_budget = 2.0


def test_frozen_screen_escalates_nonrespondable_intervention_no_bytes(tmp_path):
    # F3: a frozen-meaning busy screen (stale) escalates a NON-respondable intervention_required —
    # not a waiting_for_user, and the daemon types NOTHING (no ESC). It never sticks /status pending.
    sess, ev = _session(tmp_path, ["working… esc to interrupt"] * 8, spec=InterventionSpec())
    sess._loop()
    inter = [e for e in ev._events if e.kind == "intervention_required"]
    assert inter, "a stale frozen screen must escalate intervention_required"
    assert all(e.requires_response is False for e in inter)   # non-respondable advisory
    assert sess._decision is None                            # NOT pending -> /status never sticks
    assert ev.pending("s1") is None                          # not in the respondable pending queue
    assert sess._handle.writes == []                         # no ESC / no bytes written
    assert sess._state == "intervention_required"


def test_respond_answers_and_appends_user_input(tmp_path):
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()                                # a real respond runs while the monitor is live
    pend = ev.pending("s1")
    did = sess._decision["decision_id"]
    assert did.startswith("dec-")
    offset_before = sess._dialog.last_user_input_offset()
    # Approach B: the MONITOR writes "1"+Enter (box still free_text), the echo appears, then the turn
    # moves -> confirmed. respond() binds to the named pending decision and relays the monitor's outcome.
    out = respond_via_submit_monitor(sess, "1", did, [_BOX, _echo("1"), _WORKING])
    assert out.status == "resumed" and out.seq == pend.seq and out.decision_id == did
    assert ev.pending("s1") is None                       # answered
    # a confirmed respond appends a user_input marker (flat-log model)
    assert sess._dialog.last_user_input_offset() > offset_before
    assert sess.snapshot().get("decision") is None        # cleared
    assert "\r" in sess._handle.writes and any("1" in w for w in sess._handle.writes)


_MODAL = ("How should T7 handle the table?\n❯ 1. Enrich all three\n  2. Verify-only\n"
          "  3. Enrich establish_phase only\nEnter to select · ↑/↓ to navigate\n")


def test_modal_decision_carries_options_and_prompt_kind(tmp_path):
    sess, ev = _session(tmp_path, ["working esc to interrupt", _MODAL, _MODAL])
    sess._loop()
    dec = sess.snapshot()["decision"]
    assert dec["prompt_kind"] == "modal_choice"
    assert [o["id"] for o in dec["options"]] == ["1", "2", "3"]
    assert dec["options"][0]["label"] == "Enrich all three"


def test_respond_to_modal_routes_to_select_option(monkeypatch, tmp_path):
    monkeypatch.setattr("daemon.session.time.sleep", lambda *_: None)
    sess, ev = _session(tmp_path, ["working esc to interrupt", _MODAL, _MODAL])
    sess._loop()
    out = sess.respond("1", decision_id=sess._decision["decision_id"])
    assert out.status == "resumed"
    # select_option emits the digit + submit key as ONE sequence (driver owns the keys) — NOT the
    # free-text path of type-then-enter. This is the F2 fix: a selector, not prose.
    assert "1\r" in sess._handle.writes
    # the chosen option's LABEL is recorded in the transcript (not the bare id).
    assert "Enrich all three" in sess._dialog.page()["text"]
    assert ev.pending("s1") is None


def test_respond_to_modal_rejects_unknown_option_keeps_pending(tmp_path):
    sess, ev = _session(tmp_path, ["working esc to interrupt", _MODAL, _MODAL])
    sess._loop()
    writes_before = list(sess._handle.writes)
    out = sess.respond("9", decision_id=sess._decision["decision_id"])   # not an option id
    assert out.status == "invalid_option"
    assert sess._decision is not None                    # decision stays pending
    assert ev.pending("s1") is not None
    assert sess._handle.writes == writes_before          # NOTHING typed into the menu


def test_respond_to_free_text_uses_submit_text_not_select(tmp_path):
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()                                # a real respond runs while the monitor is live
    assert sess.snapshot()["decision"]["prompt_kind"] == "free_text"
    out = respond_via_submit_monitor(sess, "do the next thing", sess._decision["decision_id"],
                                     [_BOX, _WORKING])
    assert out.status == "resumed"
    # free-text: the text is typed then Enter pressed separately (two writes), not a select_option.
    assert "do the next thing" in sess._handle.writes and "\r" in sess._handle.writes


def test_new_decision_supersedes_and_resolves_prior(tmp_path):
    # IMPORTANT 1: when a genuinely new respondable decision (different decision_key) supersedes a
    # still-pending prior one, the prior's events must be resolved (superseded) so EventQueue.pending()
    # returns the NEW decision and never resurrects the old one.
    sess, ev = _session(tmp_path, ["screen"])
    sess._publish("waiting_for_user", hint=None, hung=False, requires_response=True, decision_key="k-A")
    a_id = sess._decision["decision_id"]
    a_events = [e for e in ev._events if e.decision_id == a_id]
    assert a_events and all(e.resolved_reason is None for e in a_events)
    sess._publish("waiting_for_user", hint=None, hung=False, requires_response=True, decision_key="k-B")
    b_id = sess._decision["decision_id"]
    assert b_id != a_id                                            # a genuinely new decision
    assert all(e.resolved_reason == "superseded" for e in a_events)  # prior decision resolved
    pend = ev.pending("s1")
    assert pend is not None and pend.decision_id == b_id          # B pending; A not resurrected


def test_respond_with_no_pending_decision_is_rejected(tmp_path):
    # No decision pending (agent still working) -> respond is a no-op, nothing typed.
    sess, _ = _session(tmp_path, ["compiling…", "compiling…"])
    out = sess.respond("1")
    assert out.status == "no_pending" and out.seq is None
    assert not sess._handle.writes


def test_respond_claims_decision_atomically_no_double_type(tmp_path):
    # The monitor claims the decision when it writes, so a second respond finds nothing pending and does
    # NOT type again (PTY input is non-idempotent — a duplicate would inject a stray line).
    sess, _ = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()                                # a real respond runs while the monitor is live
    assert respond_via_submit_monitor(sess, "1", sess._decision["decision_id"],
                                      [_BOX, _WORKING]).status == "resumed"
    writes_after_first = list(sess._handle.writes)
    assert sess.respond("2").status == "no_pending"       # already claimed (monitor cleared _decision)
    assert sess._handle.writes == writes_after_first      # nothing typed the second time


class WedgedWriteHandle:
    """A handle whose BOUNDED write times out — models an executor that stopped draining its
    stdin (the PTY input buffer filled). An unbounded write (timeout=None) would block forever."""
    def __init__(self):
        self.writes = []

    def write(self, data, timeout=None, drain_output=False):
        from daemon.errors import PtyWriteTimeout
        if timeout is not None:
            raise PtyWriteTimeout(0, len(data.encode()))
        self.writes.append(data)

    def is_alive(self):
        return True

    def exit_code(self):
        return None


def test_respond_write_is_bounded_and_reports_timeout(tmp_path):
    # The monitor's PTY write is deadline-bounded: a wedged executor (not draining stdin) must NOT hang
    # the drain -> a 'write_timeout' outcome, the decision claimed (not half-pending), nothing appended.
    sess, _ = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()
    offset_before = sess._dialog.last_user_input_offset()
    sess._handle = WedgedWriteHandle()              # executor stops draining stdin
    out = respond_via_submit_monitor(sess, "1", sess._decision["decision_id"], [_BOX])
    assert out.status == "write_timeout"            # bounded, not a hung drain
    assert sess._decision is None                   # decision was claimed, not left half-pending
    # transcript must NOT have a new user marker when the write timed out
    assert sess._dialog.last_user_input_offset() == offset_before


def test_respond_free_text_reports_respond_failed_when_submit_unconfirmed(tmp_path):
    # nelix-sud: the answer echoed but the Enter never landed — it sits stranded in the box. The
    # monitor must NOT claim a false 'resumed'; with no echo-cleared / moved evidence by the confirm
    # deadline it returns 'respond_failed' so the daemon-owned next_action tells Hermes to recover.
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()                               # a real respond runs while the monitor is live
    assert sess.snapshot()["decision"]["prompt_kind"] == "free_text"
    offset_before = sess._dialog.last_user_input_offset()
    # [box -> monitor writes] then the answer stays stranded in the box forever (Enter dropped): it
    # echoes but never clears / moves, so the confirm window elapses with no landed-submit evidence.
    out = respond_via_submit_monitor(sess, "do the next thing", sess._decision["decision_id"],
                                     [_BOX, _echo("do the next thing")])
    assert out.status == "respond_failed"
    assert out.snapshot is not None                  # carries the snapshot for the recovery path
    assert sess._decision is None                    # claimed, not left half-pending
    # an unconfirmed submit did NOT advance the transcript (the answer never went through)
    assert sess._dialog.last_user_input_offset() == offset_before


def test_respond_does_not_confirm_on_ambiguous_unknown_frame(tmp_path):
    # An ambiguous 'unknown' redraw frame (❯ with no footer) is NOT a turn-start signal — the monitor
    # must keep waiting, see the stranded echo, and fail. (A broad 'not free_text' confirm would have
    # falsely confirmed off the unknown frame.)
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()
    out = respond_via_submit_monitor(sess, "do the next thing", sess._decision["decision_id"],
                                     [_BOX, _UNKNOWN, _echo("do the next thing")])
    assert out.status == "respond_failed"


def test_respond_does_not_confirm_on_a_blank_post_write_frame_then_stranded_echo(tmp_path):
    # A blank / pre-echo confirm frame right after the monitor's write must not read as 'answer left the
    # box'. Once the dropped-Enter echo renders and stays, the submit is reported unconfirmed.
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()
    out = respond_via_submit_monitor(sess, "do the next thing", sess._decision["decision_id"],
                                     [_BOX, _BOX, _echo("do the next thing")])
    assert out.status == "respond_failed"            # not a false 'resumed' off the blank confirm frame


def test_respond_free_text_reports_respond_failed_when_answer_never_echoes(tmp_path):
    # The dropped-submit class the confirm exists to catch: the answer NEVER appears in the box (empty
    # frame the WHOLE confirm window) and the turn never moves -> no positive post-write evidence. The
    # submit is UNCONFIRMED -> 'respond_failed' (recover), never a false 'resumed'. A false confirm here
    # would arm post-submit suppression on a never-delivered answer and silence every wake.
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()                               # a real respond runs while the monitor is live
    assert sess.snapshot()["decision"]["prompt_kind"] == "free_text"
    offset_before = sess._dialog.last_user_input_offset()
    out = respond_via_submit_monitor(sess, "do the next thing", sess._decision["decision_id"], [_BOX])
    assert out.status == "respond_failed"            # NOT a false 'resumed' off the empty box
    assert out.snapshot is not None                  # carries the snapshot for the recovery path
    assert sess._decision is None                    # claimed, not left half-pending
    # an unconfirmed submit must NOT advance the transcript (no user marker off a never-landed answer)
    assert sess._dialog.last_user_input_offset() == offset_before
    # ...but the answer WAS written, so the engine recorded the submit at WRITE time (Approach B +
    # Finding 2): post-submit echo-suppression is armed when the monitor types, not at confirm-time.
    # (The grace is bounded, so a never-landed answer only DELAYS the next idle wake, never loses it.)
    assert sess._engine._post_submit_active is True


def test_respond_free_text_resumes_when_answer_leaves_box(tmp_path):
    # The happy path: the submit lands, the answer echoes then leaves the box -> confirmed -> 'resumed'.
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()                               # a real respond runs while the monitor is live
    offset_before = sess._dialog.last_user_input_offset()
    out = respond_via_submit_monitor(sess, "do the next thing", sess._decision["decision_id"],
                                     [_BOX, _echo("do the next thing"), _WORKING])
    assert out.status == "resumed"
    assert ev.pending("s1") is None
    assert sess._dialog.last_user_input_offset() > offset_before   # confirmed -> marker appended


def test_free_text_respond_never_calls_the_live_renderer_on_the_rpc_thread(tmp_path):
    """nelix-s7v Approach B AC (mirrors nelix-5r3's modal-path spy): the RPC thread running respond()
        must NEVER call _handle.render() — PtySession.pump() mutates that renderer via _feed() OUTSIDE
        Session._lock, so an RPC-thread render() would race it. With Approach B respond() only enqueues
        _pending_submit and waits; the MONITOR owns the write AND the confirm-observation, so the RPC
        thread renders ZERO times. Spy render() and assert the whole answer path makes 0 render calls."""
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()                                # a real respond runs while the monitor is live
    dec = sess.snapshot()["decision"]
    assert dec["prompt_kind"] == "free_text", "the box must surface as a free_text pause"
    did = dec["decision_id"]
    calls = {"n": 0}
    _real_render = sess._handle.render

    def spying_render():
        calls["n"] += 1
        return _real_render()
    sess._handle.render = spying_render
    try:
        out = respond_via_submit_monitor(sess, "do the next thing", did, [_BOX, _WORKING])
    finally:
        sess._handle.render = _real_render            # restore regardless of outcome
    assert calls["n"] == 0, (
        f"respond() must not render on the RPC thread (made {calls['n']} render call(s))")
    assert out.status == "resumed", f"the answer must confirm off the post-write moved frame (got {out.status})"


def test_free_text_submit_aborts_on_a_pre_submit_moved_frame_without_typing(tmp_path):
    """nelix-s7v Approach B staleness AC: the monitor writes ONLY if the free_text prompt is still on
        screen. If the very first frame it observes for a queued submit is already a 'moved' state (the
        screen changed before the write), it ABORTS: nothing is typed, and the answer is reported stale
        — never confirmed off that pre-submit frame. Deterministic single-monitor drive."""
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()
    did = sess.snapshot()["decision"]["decision_id"]
    offset_before = sess._dialog.last_user_input_offset()
    writes_before = list(sess._handle.writes)
    out = respond_via_submit_monitor(sess, "do the next thing", did, [_WORKING])
    assert out.status == "stale", f"a pre-submit moved frame must abort, not confirm (got {out.status})"
    assert sess._handle.writes == writes_before                     # NOTHING typed
    assert sess._dialog.last_user_input_offset() == offset_before   # no marker


def test_free_text_submit_confirms_on_evidence_that_lands_right_after_the_write(tmp_path):
    """nelix-s7v Approach B false-abort AC (ports fix-wave-2's evidence-tick test): when the ONLY
        post-write evidence is the very next frame the monitor pumps after its write (the answer moved /
        exited the child immediately), the monitor MUST still confirm — its confirm reads frames it
        pumped AFTER its own write, so a single evidence tick right after the write is never missed."""
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()
    did = sess.snapshot()["decision"]["decision_id"]
    offset_before = sess._dialog.last_user_input_offset()
    # [box -> write] then a single 'moved' frame with nothing echoing first -> must confirm.
    out = respond_via_submit_monitor(sess, "do the next thing", did, [_BOX, _WORKING])
    assert out.status == "resumed", (
        f"a landed answer's first post-write frame must confirm, not false-abort (got {out.status})")
    assert sess._dialog.last_user_input_offset() > offset_before


def test_free_text_submit_does_not_confirm_off_an_unrelated_pre_submit_moved_transition(tmp_path):
    """nelix-s7v Approach B FALSE-CONFIRM AC: the monitor confirms ONLY from frames it pumped AFTER its
        own write. If the screen shows an UNRELATED 'moved' state (the turn advanced / the child exited)
        BEFORE the monitor writes, that is PRE-submit evidence and must NOT confirm the answer. Approach
        B aborts (the queued drain sees a non-free_text frame and never writes). RED on 7850b28 — the
        render-generation confirm reads the pre-submit moved frame off the cache and false-resumes;
        GREEN after (stale / not resumed). Driven through the REAL _loop_once so it exercises whichever
        confirm implementation is compiled in."""
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()
    did = sess.snapshot()["decision"]["decision_id"]
    offset_before = sess._dialog.last_user_input_offset()
    # The screen has already MOVED to a working spinner (unrelated to our answer) before the monitor can
    # write; render() returns that pre-submit 'moved' frame on every pump.
    sess._handle = FakeHandle([_WORKING])
    out = respond_driving_loop(sess, "do the next thing", did)
    assert out.status != "resumed", (
        f"must NOT confirm off a pre-submit moved frame the monitor never wrote after (got {out.status})")
    assert sess._dialog.last_user_input_offset() == offset_before   # nothing landed -> no marker


def test_write_timeout_frees_the_submit_slot_without_a_second_resolve(tmp_path):
    """nelix-s7v Approach B FINDING 1: on a write timeout the monitor must CLEAR _pending_submit (like
        the abort / confirm / unconfirmed paths). Otherwise the NEXT tick re-drains the same 'queued'
        slot with _decision now None, ABORTS it (a SECOND resolve), and overwrites the outcome
        write_timeout -> stale, so the caller can observe the wrong outcome and the slot lingers. Drive
        the drain directly (deterministic, no thread race): a wedged write -> write_timeout AND the slot
        is freed, so a following drain is a no-op. RED on 06f6e09 (slot not cleared)."""
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()
    did = sess.snapshot()["decision"]["decision_id"]
    sess._handle = WedgedWriteHandle()                # every write raises PtyWriteTimeout
    with sess._lock:                                  # enqueue exactly as _respond_free_text does
        sess._pending_submit = {"decision_id": did, "clean": "1", "event": threading.Event(),
                                "outcome": None, "seq": None, "state": "queued", "saw_echo": False,
                                "write_deadline": time.monotonic() + sess._spec.respond_write_seconds,
                                "confirm_deadline": None}
    ps = sess._pending_submit
    sess._drain_pending_submit(sess._driver.observe(_BOX, sess._obs_ctx()))  # queued -> write -> timeout
    assert ps["outcome"] == "write_timeout"
    assert sess._pending_submit is None               # FINDING 1: slot freed (RED on 06f6e09: still set)
    # a following tick must be a no-op — it must NOT re-resolve the decision or overwrite the outcome
    sess._drain_pending_submit(sess._driver.observe(_BOX, sess._obs_ctx()))
    assert ps["outcome"] == "write_timeout"           # unchanged (RED on 06f6e09: overwritten to 'stale')


def test_free_text_submit_written_then_modal_frame_does_not_orphan_the_modal(tmp_path):
    """nelix-s7v Approach B FINDING 2 (ordering): _loop_once runs engine.tick() BEFORE
        _drain_pending_submit. After a successful write, the NEXT frame can be a modal — engine.tick()
        publishes it IMMEDIATELY (high-confidence), THEN the drain confirms the free-text submit. If
        on_submit runs at confirm-time it CLEARS the engine's just-published modal key, so when the
        modal later disappears the engine cannot withdraw it -> a STALE pending decision sticks. The
        fix records the submit (on_submit) at WRITE time, before that next tick's engine.tick(). Driven
        through the REAL _loop_once. RED on 06f6e09 (stale modal decision sticks); GREEN after."""
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()
    did = sess.snapshot()["decision"]["decision_id"]
    # box (write) -> a modal on the NEXT tick (engine publishes it + drain confirms) -> modal disappears
    sess._handle = FakeHandle([_BOX, _MODAL, _WORKING, _WORKING, _WORKING, _WORKING])
    box = {}

    def rpc():
        try:
            box["out"] = sess.respond("do the next thing", decision_id=did)
        except BaseException as exc:        # pragma: no cover
            box["err"] = exc
    t = threading.Thread(target=rpc, daemon=True)
    t.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:      # wait for the submit to enqueue
        with sess._lock:
            if sess._pending_submit is not None:
                break
        time.sleep(0)
    for _ in range(6):                      # tick1 box->write, tick2 modal->publish+confirm, tick3+ gone
        sess._loop_once()
        time.sleep(0.002)
    t.join(timeout=3.0)
    if "err" in box:
        raise box["err"]
    assert box["out"].status == "resumed"                    # confirmed off the modal (the turn moved)
    # the modal the engine published AFTER the write must still be withdrawable once it disappears —
    # on_submit must NOT have clobbered its published key at confirm-time.
    assert sess.snapshot().get("decision") is None, (
        "a modal published after the write was orphaned (on_submit ran at confirm-time and cleared its key)")
    assert ev.pending("s1") is None


def test_answering_reemitted_blocked_clears_pending(tmp_path):
    # Answering a decision the backstop re-emitted must clear pending() for ALL its events, not just
    # the latest — otherwise pending() surfaces a stale earlier event for the resolved decision.
    # The trust modal carries a question row (a NON-None modal_body_fp) so the blocked answer is
    # submitted — a bodyless modal is identity-ambiguous and the drain aborts it (E2).
    trust = ("Quick safety check: Is this a project you trust?\n"
             "❯ 1. Yes, I trust this folder\n  2. No, exit\nEnter to confirm\n")
    sess, handle, ev = make_session(tmp_path, frames=[trust], spec=BackstopSpec())
    sess.start("do work", str(tmp_path))
    _wait_for(lambda: sum(1 for e in ev._events if e.kind == "blocked") >= 2, timeout=3)
    assert sess.respond("1", decision_id=sess._decision["decision_id"]).status == "resumed"
    assert ev.pending("s1") is None                       # every re-emit answered
    sess.stop()


def test_respond_with_stale_decision_id_is_rejected_and_returns_current(tmp_path):
    # decision_id is an OPTIONAL guard from the status pull, not a required identity. A mismatch
    # is rejected as stale and the response carries the current pending decision for reconcile;
    # the correct id (or no id) still works.
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._loop()
    sess._stop.clear()                                # a real respond runs while the monitor is live
    cur = sess._decision["decision_id"]
    out = sess.respond("1", decision_id="dec-stale99")   # wrong id: rejected before any enqueue/type
    assert out.status == "stale"
    assert out.pending["decision_id"] == cur and out.pending["kind"] == "waiting_for_user"
    assert out.pending["text"]                            # frozen question text in the stale body too
    assert ev.pending("s1") is not None                   # NOT answered
    assert not sess._handle.writes                        # nothing typed
    assert respond_via_submit_monitor(sess, "1", cur, [_BOX, _WORKING]).status == "resumed"  # correct id works


def test_respond_without_decision_id_is_missing_not_guessed(tmp_path):
    # A respondable pending decision answered WITHOUT decision_id must be refused as
    # missing_decision_id — the daemon must NOT guess which question the answer binds to (the
    # s-9c0b6eeb incident: a bare answer leaked into the agent's prompt). The pending meta comes
    # back so Hermes can retry with the id, without a separate nelix_status pull.
    box = "Ready — what next?\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"
    sess, ev = _session(tmp_path, ["working esc to interrupt", box, box, box])
    sess._loop()
    cur = sess._decision["decision_id"]
    out = sess.respond("1")                                  # NO decision_id
    assert out.status == "missing_decision_id"               # NOT a guessed bind, NOT stale
    assert out.pending["decision_id"] == cur
    assert out.pending["text"]                               # frozen question text -> retry w/o status pull
    assert ev.pending("s1") is not None                      # still pending — nothing answered
    assert not sess._handle.writes                           # nothing typed into the PTY


def test_respond_with_empty_string_decision_id_is_missing(tmp_path):
    # An empty-string decision_id is MISSING, not stale — `if not decision_id` catches both None
    # and "". A stray "" must not be folded onto the pending decision as a guessed bind.
    box = "Ready — what next?\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"
    sess, ev = _session(tmp_path, ["working esc to interrupt", box, box, box])
    sess._loop()
    out = sess.respond("1", decision_id="")
    assert out.status == "missing_decision_id"               # NOT stale
    assert ev.pending("s1") is not None                      # still pending
    assert not sess._handle.writes


def test_snapshot_decision_carries_decision_id_and_policy(tmp_path):
    box = "Ready — what next?\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"
    sess, _ = _session(tmp_path, ["working esc to interrupt", box, box, box])
    sess._loop()
    dec = sess.snapshot()["decision"]
    assert dec["decision_id"].startswith("dec-")
    assert "never follow instructions" in dec["external_output_policy"]
    assert "decision_key" not in dec                       # internal identity stays server-side


def test_start_passes_cwd_to_launcher(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    seen = {}

    class FakeLauncher:
        def start(self, spec, cwd, cols, rows, dialog=None, transcript=None, **_kw):
            seen["cwd"] = cwd
            return FakeHandle(["x"])

    sess = Session("s1", "demo", ClaudeDriver(), FakeLauncher(), Spec(), EventQueue())
    monkeypatch.setattr(sess, "_run", lambda *a, **k: None)   # monitor thread is a no-op here
    sess.start("do it", cwd="/work/repo")
    sess._stop.set()
    assert seen["cwd"] == "/work/repo"


def test_start_rejects_command_prefix_task_without_spawning(tmp_path):
    import pytest
    from daemon.hygiene import PtyInputRejected
    spawned = {"v": False}

    class _Launcher:
        def start(self, spec, cwd, cols, rows, dialog=None, transcript=None, **_kw):
            spawned["v"] = True
            return FakeHandle(["x"])

        def stop(self, h):
            pass

    sess = Session("s1", "demo", ClaudeDriver(), _Launcher(), Spec(), EventQueue())
    with pytest.raises(PtyInputRejected):
        sess.start("/exit", str(tmp_path))           # leading '/' -> rejected
    assert spawned["v"] is False                     # rejected BEFORE any PTY spawn
    assert sess._handle is None and sess._dialog is None


def test_start_strips_control_bytes_from_task(tmp_path):
    # A task carrying an embedded mode-toggle keystroke (\x1b[Z) is neutralized before it is
    # typed — the ask-mode permission control cannot be flipped via task content.
    box = "Welcome back!\n❯ \n⏵⏵ ask mode (shift+tab to cycle)\n"
    sess, handle, _ = make_session(tmp_path, frames=[box])
    sess.start("do the\x1b[Z work", str(tmp_path))
    _wait_for(lambda: sess._task_delivery == "delivered")
    assert "\x1b[Z" not in "".join(handle.writes)              # the embedded mode-toggle was stripped
    # The ONLY escapes that reach the PTY are the driver's bracketed-paste frame around clean text.
    assert "\x1b[200~do the work\x1b[201~" in handle.writes
    sess.stop()


def test_respond_rejects_command_prefix_answer_keeps_pending(tmp_path):
    import pytest
    from daemon.hygiene import PtyInputRejected
    trust = "❯ 1. Yes, I trust this folder\n  2. No, exit\nEnter to confirm\n"
    sess, handle, ev = make_session(tmp_path, frames=[trust])
    sess.start("do work", str(tmp_path))
    _wait_for(lambda: sess._decision and sess._decision["kind"] == "blocked")
    writes_before = list(handle.writes)
    with pytest.raises(PtyInputRejected):
        sess.respond("/etc/passwd", decision_id=sess._decision["decision_id"])  # leading '/' -> rejected
    assert ev.pending("s1") is not None              # NOT marked answered: still pending
    assert sess._decision is not None                # decision not cleared
    assert handle.writes == writes_before            # nothing typed
    sess.stop()


def _seed_pending_decision(sess, decision_id="dec-t"):
    sess._decision = {"kind": "waiting_for_user", "hint": None, "hung": False,
                      "text": "?", "requires_response": True, "options": [],
                      "prompt_kind": "free_text", "decision_key": "k",
                      "decision_id": decision_id, "event_id": "e1", "seq": 1}


def test_respond_resumes_to_busy_without_echoed_decision(tmp_path):
    # The answer LEAVES the box (the turn moves to working) -> POSITIVE post-write evidence -> resumes
    # to busy. (A bare empty box that NEVER echoed is NOT a confirmed submit -> respond_failed.)
    sess, _ = _session(tmp_path, [_BOX])
    _seed_pending_decision(sess)
    out = respond_via_submit_monitor(sess, "do that", sess._decision["decision_id"],
                                     [_BOX, _echo("do that"), _WORKING])
    assert out.status == "resumed"
    assert out.answered_decision_id == "dec-t"
    assert sess._state == "busy"                       # Invariant A: resumed -> working again
    assert out.snapshot["control_state"] == "busy"
    assert out.snapshot["pending"] is False
    assert "decision" not in out.snapshot
    assert "still working" in out.snapshot["message"].lower()


def test_delivery_never_forces_permission_mode(monkeypatch, tmp_path):
    # Dumb-bridge invariant (nelix-zl9): the daemon must not toggle the executor's
    # permission mode. Even when the executor sits in an auto/accept-edits footer
    # (which the old pre-nelix-zl9 code treated as needing correction and tried to cycle),
    # delivery must send ZERO Shift+Tab (\x1b[Z) sequences.
    monkeypatch.setattr("daemon.session.time.sleep", lambda *_: None)
    # A ready free-text prompt whose footer is an AUTO mode (would have triggered the toggle).
    # No real capture reaches delivery while in auto/accept-edits mode (the whole golden corpus
    # is captured in ask mode), so this drives the in-process replay harness with a realistic
    # synthetic auto-mode footer frame instead (nelix-wtx documented-gap precedent).
    frame = "Here is my answer.\n❯ \n⏵⏵ accept edits on (shift+tab to cycle)"
    sess, _ = _session(tmp_path, [frame])
    monkeypatch.setattr(sess, "_deliver_task", lambda: None)   # isolate: no confirm-wait needed
    sess._delivery_tick(frame)
    assert "\x1b[Z" not in "".join(sess._handle.writes)


# ---- live (real-thread) start/delivery harness -------------------------------
# Drives Session through the real monitor thread (Session.start spawns it). The
# handle below records writes and simulates echo so delivery/blocked can be observed.

class LiveHandle:
    def __init__(self, frames, dialog=None):
        self._frames = list(frames)      # list[str]; last one repeats
        self._i = 0
        self.writes = []
        self._dialog = dialog

    def pump(self, timeout=0.1):
        if self._i < len(self._frames) - 1:
            self._i += 1
        time.sleep(0.005)                # yield so real-time polling can observe steps
        return True

    def render(self):
        return self._frames[min(self._i, len(self._frames) - 1)]

    def write(self, data, timeout=None, drain_output=False):
        self.writes.append(data)
        # simulate echo: typing text makes it visible in the (current) frame
        if data not in ("\r", "\x1b[Z", "\x1b"):
            j = min(self._i, len(self._frames) - 1)
            self._frames[j] = self._frames[j].replace("❯ \n", f"❯ {data}\n")

    def is_alive(self):
        return True

    def exit_code(self):
        return None

    def finalize(self):
        dialog = getattr(self, '_dialog', None)
        if dialog is not None:
            for ln in self.render().splitlines():
                t = ln.rstrip()
                if t:
                    dialog.add_agent_line(t)

    def advance_to_input_box(self):
        self._i = len(self._frames) - 1

    def leader_pid(self): return 4242
    def leader_pgid(self): return 4242
    def assert_leader_is_group_leader(self):
        pid, pgid = self.leader_pid(), self.leader_pgid()
        if pid is None or pid != pgid:
            raise RuntimeError(f"pty leader {pid} is not its own group leader (pgid={pgid})")
    def leader_status(self):
        from daemon.launchers.base import LeaderStatus
        return LeaderStatus(alive=True, exit_code=None, signal=None, status_available=False)

    def close(self):
        pass


class PasteCollapseHandle(LiveHandle):
    """Claude collapses a long pasted task into a "[Pasted text #N]" placeholder instead of echoing."""
    def write(self, data, timeout=None, drain_output=False):
        self.writes.append(data)
        if data not in ("\r", "\x1b[Z", "\x1b"):
            j = min(self._i, len(self._frames) - 1)
            self._frames[j] = self._frames[j].replace("❯ \n", "❯ [Pasted text #1]\n")


def make_session(tmp_path, frames, handle_cls=LiveHandle, spec=None, logger=_REAL_LOGGER):
    if logger is _REAL_LOGGER:
        logger = default_logger()          # real Logger by default -> _run/_loop log sites fire
    ev = EventQueue()
    handle = handle_cls(list(frames))

    class _Launcher:
        def start(self, spec, cwd, cols, rows, dialog=None, transcript=None, **_kw):
            handle._dialog = dialog
            return handle

        def stop(self, h):
            pass

    sess = Session("s1", "demo", ClaudeDriver(), _Launcher(), spec or Spec(), ev, logger=logger)
    return sess, handle, ev


def _wait_for(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_start_is_async_and_delivers_when_input_box_ready(tmp_path):
    box = "Welcome back!\n❯ \n⏵⏵ ask mode (shift+tab to cycle)\n"
    sess, handle, _ = make_session(tmp_path, frames=[box])
    sess.start("create report.md", str(tmp_path))   # must return immediately
    assert sess._task_delivery in ("pending", "delivered")   # did not block on delivery
    _wait_for(lambda: sess._task_delivery == "delivered")
    assert sess._task_delivery == "delivered"
    assert "create report.md" in "".join(handle.writes)   # typed
    assert "\r" in handle.writes                          # then Enter
    assert handle.writes.index("\r") > 0                  # Enter AFTER typing
    sess.stop()


def test_session_start_writes_meta_json(monkeypatch, tmp_path):
    # nelix-capture needs the exact cols/rows the raw was captured at (replaying at the wrong size
    # reflows differently) -> persist them (+ executor/driver) at session start, with private perms.
    import json
    import stat
    import paths
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sess, _, _ = make_session(tmp_path, frames=["Welcome\n❯ \n⏵⏵ ask mode (shift+tab to cycle)\n"])
    sess.start("do work", str(tmp_path))
    meta_path = paths.sessions_root() / "s1" / "meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text())
    assert meta["cols"] == 120 and meta["rows"] == 40
    assert meta["executor"] == "demo" and meta["driver"] == "claude"
    assert stat.S_IMODE(meta_path.stat().st_mode) == 0o600   # same discipline as transcript/raw
    sess.stop()


def test_blocked_on_trust_menu_types_nothing(tmp_path):
    trust = ("Quick safety check: Is this a project you created or one you trust?\n"
             "❯ 1. Yes, I trust this folder\n  2. No, exit\n"
             "Enter to confirm · Esc to cancel\n")
    sess, handle, _ = make_session(tmp_path, frames=[trust])
    sess.start("do work", str(tmp_path))
    _wait_for(lambda: sess._decision is not None and sess._decision["kind"] == "blocked")
    assert sess._task_delivery == "pending"
    # nothing is typed on a modal: no task text, no Enter, no mode-toggle bytes
    assert handle.writes == []
    sess.stop()


def test_held_task_delivers_after_interstitials_clear(tmp_path):
    trust = "❯ 1. Yes, I trust this folder\n  2. No, exit\nEnter to confirm\n"
    box = "Welcome back!\n❯ \n⏵⏵ ask mode (shift+tab to cycle)\n"
    sess, handle, _ = make_session(tmp_path, frames=[trust, box])
    sess.start("do work", str(tmp_path))
    _wait_for(lambda: sess._decision and sess._decision["kind"] == "blocked")
    handle.advance_to_input_box()                  # simulate the menu being answered
    _wait_for(lambda: sess._task_delivery == "delivered")
    assert "do work" in "".join(handle.writes) and "\r" in handle.writes
    sess.stop()


def test_delivery_confirms_when_claude_collapses_paste(tmp_path):
    box = "Welcome back!\n❯ \n⏵⏵ ask mode (shift+tab to cycle)\n"
    sess, handle, _ = make_session(tmp_path, frames=[box], handle_cls=PasteCollapseHandle)
    sess.start("a long multi-paragraph task that claude will collapse into a paste", str(tmp_path))
    _wait_for(lambda: sess._task_delivery == "delivered", timeout=5)
    assert sess._task_delivery == "delivered"
    assert "\r" in handle.writes                                          # Enter pressed
    task_writes = [w for w in handle.writes if "multi-paragraph" in w]
    assert len(task_writes) == 1                                          # typed exactly once
    sess.stop()


def test_delivery_wraps_task_in_bracketed_paste(tmp_path):
    # nelix-10z: the claude driver delivers the task as ONE bracketed paste so Claude collapses it
    # to a placeholder instead of re-rendering every char (0.0s vs 2.2s for 61.5KB). The markers
    # frame only the text; Enter is a SEPARATE write AFTER the paste, never inside it.
    box = "Welcome back!\n❯ \n⏵⏵ ask mode (shift+tab to cycle)\n"
    sess, handle, _ = make_session(tmp_path, frames=[box], handle_cls=PasteCollapseHandle)
    sess.start("write a big report", str(tmp_path))
    _wait_for(lambda: sess._task_delivery == "delivered", timeout=5)
    assert sess._task_delivery == "delivered"
    assert "\x1b[200~write a big report\x1b[201~" in handle.writes      # one bracketed-paste write
    assert handle.writes[-1] == "\r"                                   # Enter last, OUTSIDE the paste
    sess.stop()


def test_delivery_arms_hook_startup_grace_for_hook_capable(tmp_path):
    # CRITICAL 1 (wiring): a hook-capable driver's task-delivery must arm the belief engine's hook
    # startup grace (expect_hooks). hook_mode stays "unknown" and the screen stays conservative about
    # a screen-derived free-text idle until the first hook arrives or the grace expires (spec §6).
    box = "Welcome back!\n❯ \n⏵⏵ ask mode (shift+tab to cycle)\n"
    sess, handle, _ = make_session(tmp_path, frames=[box])
    sess.start("do the thing", str(tmp_path))
    _wait_for(lambda: sess._task_delivery == "delivered", timeout=5)
    assert sess._task_delivery == "delivered"
    assert sess._engine.hook_mode == "unknown"                 # no hook yet
    assert sess._engine._hook_startup_at is not None           # grace armed at delivery (expect_hooks)
    sess.stop()


def test_delivery_timeout_marks_failed_and_wakes(tmp_path):
    # The typed task never confirms (no echo, no placeholder). After the confirm window, delivery is
    # marked failed and a non-respondable delivery_failed event wakes Hermes — nothing is re-typed.
    class NoEchoHandle(LiveHandle):
        def write(self, data, timeout=None, drain_output=False):
            self.writes.append(data)            # record but never render evidence -> never confirms

    box = "Welcome back!\n❯ \n⏵⏵ ask mode (shift+tab to cycle)\n"
    sess, handle, ev = make_session(tmp_path, frames=[box], handle_cls=NoEchoHandle,
                                    spec=FastConfirmSpec())
    sess.start("create report.md", str(tmp_path))
    # wait for the EVENT, not just the flag: _fail_delivery flips _task_delivery before it publishes,
    # so gating on the flag alone races the publish.
    _wait_for(lambda: any(e.kind == "delivery_failed" for e in ev._events), timeout=5)
    assert sess._task_delivery == "failed"
    assert "\r" not in handle.writes                                    # never pressed Enter
    assert len([w for w in handle.writes if "report" in w]) == 1        # typed once, not re-typed
    last = ev.latest_after(0)
    assert last.kind == "delivery_failed" and last.hint == "delivery_unconfirmed"
    assert last.requires_response is False
    time.sleep(0.3)                                                     # loop has exited
    assert sess._task_delivery == "failed"                             # stays failed, no re-delivery
    sess.stop()


def test_blocked_is_not_respammed_while_screen_unchanged(tmp_path):
    trust = "❯ 1. Yes, I trust this folder\n  2. No, exit\nEnter to confirm\n"
    sess, handle, ev = make_session(tmp_path, frames=[trust])
    sess.start("do work", str(tmp_path))
    _wait_for(lambda: sess._decision and sess._decision["kind"] == "blocked")
    time.sleep(0.4)                                # let the monitor loop spin several times
    blocked = [e for e in ev._events if e.kind == "blocked"]
    assert len(blocked) == 1                        # one blocked, not per-loop spam
    sess.stop()


def test_respond_to_blocked_does_not_append_user_input(tmp_path):
    # The trust modal carries a question row (a NON-None modal_body_fp) so the blocked answer is
    # submitted — a bodyless modal is identity-ambiguous and the drain aborts it (E2).
    trust = ("Quick safety check: Is this a project you trust?\n"
             "❯ 1. Yes, I trust this folder\n  2. No, exit\nEnter to confirm\n")
    sess, handle, _ = make_session(tmp_path, frames=[trust])
    sess.start("do work", str(tmp_path))
    _wait_for(lambda: sess._decision and sess._decision["kind"] == "blocked")
    offset_before = sess._dialog.last_user_input_offset()
    assert sess.respond("1", decision_id=sess._decision["decision_id"]).status == "resumed"     # binds to the current pending decision
    assert "1\r" in handle.writes                     # answer injected via select_option (digit+CR),
    assert "1" not in handle.writes                   # NOT free-text ('1' then a bare '\r' leaks a stray digit)
    # A blocked respond must NOT append a user_input marker (flat-log model)
    assert sess._dialog.last_user_input_offset() == offset_before
    sess.stop()


def test_idle_prompt_with_footer_below_input_is_waiting_for_user(monkeypatch, tmp_path):
    # Faithful Claude layout: a question ABOVE the input line, the mode footer BELOW it. The
    # last content line is the footer, not the question — the old _has_question misread this as
    # 'attention'. Every post-delivery idle must now be waiting_for_user.
    box = ("Which database should I use?\n"
           "❯ \n"
           "⏵⏵ ask mode · shift+tab to cycle\n")
    sess, ev = _session(tmp_path, ["working esc to interrupt", box, box, box])
    sess._loop()
    dec = sess.snapshot()["decision"]
    assert dec["kind"] == "waiting_for_user" and dec["requires_response"] is True
    assert dec["prompt_kind"] == "free_text"
    assert ev.pending("s1") is not None


def test_respond_to_blocked_does_not_re_emit_same_frame(tmp_path):
    # After respond, the SAME interstitial frame must NOT spawn a second blocked event
    # (fingerprint dedup alone). A genuinely different frame still emits.
    # The trust modal carries a question row (a NON-None modal_body_fp) so the blocked answer is
    # submitted — a bodyless modal is identity-ambiguous and the drain aborts it (E2).
    trust = ("Quick safety check: Is this a project you trust?\n"
             "❯ 1. Yes, I trust this folder\n  2. No, exit\nEnter to confirm\n")
    sess, handle, ev = make_session(tmp_path, frames=[trust])
    sess.start("do work", str(tmp_path))
    _wait_for(lambda: sess._decision and sess._decision["kind"] == "blocked")
    assert sess.respond("1", decision_id=sess._decision["decision_id"]).status == "resumed"    # binds to the current pending decision
    time.sleep(0.3)                                # monitor keeps seeing the same trust frame
    blocked = [e for e in ev._events if e.kind == "blocked"]
    assert len(blocked) == 1                        # no duplicate for the unchanged frame
    sess.stop()


def test_idle_backstop_re_surfaces_unanswered_blocked(tmp_path):
    # spec §6: while pending and blocked is unanswered, a no-progress backstop re-surfaces the
    # blocked event with hung=True (bypassing the fingerprint dedup).
    trust = "❯ 1. Yes, I trust this folder\n  2. No, exit\nEnter to confirm\n"
    sess, handle, ev = make_session(tmp_path, frames=[trust], spec=BackstopSpec())
    sess.start("do work", str(tmp_path))
    _wait_for(lambda: any(e.kind == "blocked" and e.hung for e in ev._events), timeout=3)
    hung = [e for e in ev._events if e.kind == "blocked" and e.hung]
    assert hung, "expected a re-surfaced blocked event with hung=True"
    sess.stop()


def test_backstop_reemit_preserves_decision_id(tmp_path):
    # The no-progress backstop re-publishes the SAME blocked pause. event_id is notification
    # identity (changes per emit); decision_id is decision identity (stable across the reminder),
    # so a held decision_id never self-invalidates.
    trust = "❯ 1. Yes, I trust this folder\n  2. No, exit\nEnter to confirm\n"
    sess, handle, ev = make_session(tmp_path, frames=[trust], spec=BackstopSpec())
    sess.start("do work", str(tmp_path))
    _wait_for(lambda: sess._decision and sess._decision["kind"] == "blocked")
    first_obj = sess._decision
    first_did = sess._decision["decision_id"]
    _wait_for(lambda: any(e.kind == "blocked" and e.hung for e in ev._events), timeout=3)
    assert sess._decision["decision_id"] == first_did                 # stable across re-emit
    assert sess._decision is first_obj                                # updated IN PLACE, not swapped
    assert sess._decision["hung"] is True                             # hung refreshed on re-emit
    eids = {e.event_id for e in ev._events if e.kind == "blocked"}
    assert len(eids) >= 2                                             # distinct notification ids
    sess.stop()


def test_reemit_install_after_claim_does_not_resurrect_answered_decision(monkeypatch, tmp_path):
    # The race Codex flagged: a re-emit is BUILT while the decision is pending, but its install hook
    # runs AFTER a concurrent respond() claimed/answered it. The hook must NOT resurrect the answered
    # decision, and must mark the now-obsolete re-emit event answered (so pending() stays clean).
    box = "Ready — what next?\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"
    # logger=None ON PURPOSE: this test stubs ev.publish to RETURN None (the `defer` below) to capture
    # a re-emit without publishing it — a degenerate stub the real EventQueue.publish never does (it
    # always returns an Event). _publish then logs audit_decision(evt.event_id); with a real Logger that
    # would raise on the None `evt`, but that is the stub's artifact, not a prod path (real publish is
    # non-None, so evt.event_id is always safe). The real-Logger default is exercised by the other
    # _session tests; this one keeps the None-guard so the None-returning stub is tolerated.
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX], logger=None)
    sess._loop()
    sess._stop.clear()                                # a real respond runs while the monitor is live
    key = sess._decision["decision_key"]
    real_publish = ev.publish
    deferred = {}

    def defer(*a, **k):
        deferred["a"], deferred["k"] = a, k        # capture the re-emit; do NOT publish yet
        return None
    monkeypatch.setattr(ev, "publish", defer)
    sess._publish("waiting_for_user", hint=None, hung=True, requires_response=True, decision_key=key)
    monkeypatch.setattr(ev, "publish", real_publish)
    # the monitor claims + answers (writes + confirms) BEFORE the re-emit is published
    assert respond_via_submit_monitor(sess, "1", sess._decision["decision_id"],
                                      [_BOX, _WORKING]).status == "resumed"
    assert sess._decision is None
    real_publish(*deferred["a"], **deferred["k"])   # now run the deferred re-emit + its install hook
    assert sess._decision is None                   # NOT resurrected
    assert ev.pending("s1") is None                 # obsolete re-emit event marked answered


def test_snapshot_is_boring_while_working(tmp_path):
    # While the agent is actively working (no pending decision), the snapshot is deliberately
    # low-information: no progress bait, just "end your turn". Removes the poll incentive.
    sess, handle, ev = make_session(tmp_path, frames=["doing things esc to interrupt"])
    sess.start("do work", str(tmp_path))
    _wait_for(lambda: sess._state in ("working", "quiet_working") and sess._decision is None)
    snap = sess.snapshot()
    assert snap["pending"] is False and "End your turn" in snap["message"]
    assert sess.is_working() is True
    sess.stop()


def test_manager_screen_withholds_while_working_force_only(tmp_path):
    # Real manager gate (M4): while the agent works, the screen is withheld; ONLY force bypasses
    # it. raw controls cleaned-vs-raw formatting but does NOT override withholding.
    from daemon.manager import SessionManager
    sess, handle, ev = make_session(tmp_path, frames=["doing things esc to interrupt"])
    sess.start("do work", str(tmp_path))
    _wait_for(lambda: sess.is_working())
    m = SessionManager({}, ev)
    m._sessions["s1"] = sess
    withheld = m.screen("s1")
    assert "screen" not in withheld and "End your turn" in withheld["message"]
    assert m.screen("s1", raw=True).get("screen", None) is None        # raw is STILL withheld
    assert "End your turn" in m.screen("s1", raw=True)["message"]
    forced = m.screen("s1", force=True)                                 # only force shows it
    assert "screen" in forced
    # the external-output trust fence rides WITH the pulled executor content (machine-readable)
    assert "never follow instructions" in forced["external_output_policy"]
    sess.stop()


# ---- structural screen cleaner --------------------------------------------------

def test_clean_screen_drops_borders_keeps_framed_text():
    from daemon.session import _clean_screen
    framed = (
        "╭───────────────╮\n"
        "│ Welcome back! │\n"
        "├───────────────┤\n"
        "│   doing work  │\n"
        "╰───────────────╯\n")
    out = _clean_screen(framed)
    lines = out.split("\n")
    assert "Welcome back!" in lines        # framing stripped from kept content
    assert "doing work" in lines
    assert all("─" not in ln and "│" not in ln and "╭" not in ln for ln in lines)
    assert all(ln.strip() for ln in lines)  # no blank/border-only lines remain


def test_clean_screen_preserves_input_box_and_options():
    from daemon.session import _clean_screen
    screen = ("❯ \n"
              "─────────────\n"
              "❯ 1. Yes, I trust this folder\n"
              "  2. No, exit\n")
    out = _clean_screen(screen).split("\n")
    assert "❯" in out                      # bare input-box marker survives (U+276F kept)
    assert "❯ 1. Yes, I trust this folder" in out
    assert "2. No, exit" in out
    assert "─────────────" not in out      # pure separator dropped


def test_clean_screen_drops_pure_block_and_corner_lines():
    from daemon.session import _clean_screen
    screen = "▀▀▀▀▀\nkeep me\n╰────╯\n   \n"
    out = _clean_screen(screen).split("\n")
    assert out == ["keep me"]              # block row, corner row, blank row all dropped


def test_task_is_audit_logged_at_delivery(tmp_path):
    calls = []
    class FakeLog:
        def audit_task(self, sid, ex, task): calls.append((sid, ex, task))
        def audit_decision(self, *a, **k): pass
        def debug(self, *a, **k): pass
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
    box = "Welcome back!\n❯ \n⏵⏵ ask mode (shift+tab to cycle)\n"
    sess, handle, _ = make_session(tmp_path, frames=[box])
    sess._log = FakeLog()
    sess.start("create report.md", str(tmp_path))
    _wait_for(lambda: sess._task_delivery == "delivered")
    assert calls and calls[0][2] == "create report.md"
    sess.stop()


def test_session_screen_cleans_by_default_and_raw_is_exact(tmp_path):
    raw = "╭──────╮\n│ hi  │\n╰──────╯\n"
    sess, handle, _ = make_session(tmp_path, frames=[raw])
    sess._handle = handle                  # render() serves the framed frame
    from daemon.session import _clean_screen
    assert sess.screen() == _clean_screen(raw)
    assert sess.screen(raw=True) == raw    # exact untouched render()
    assert "│" in sess.screen(raw=True) and "│" not in sess.screen()


# ---- observability: finalization correctness (Task 5) ------------------------

def _bare_session(tmp_path, handle, logger=_REAL_LOGGER):
    if logger is _REAL_LOGGER:
        logger = default_logger()          # real Logger by default -> _run/_finish log sites fire
    ev = EventQueue()
    sess = Session("s1", "demo", ClaudeDriver(), None, Spec(), ev, logger=logger)
    sess._handle = handle
    sess._dialog = Dialog(tmp_path / "s1", tail_lines=Spec.tail_lines,
                          spool_max_bytes=Spec.spool_max_bytes)
    sess._spawn_ts = 0.0
    return sess, ev


def test_pre_delivery_death_publishes_and_sets_state(tmp_path):
    """The incident: child dies while delivery is pending -> terminal event + state, not silence."""
    sess, ev = _bare_session(tmp_path, DeadHandle(0))
    sess._run()
    assert sess.snapshot()["control_state"] == "terminal"  # was state="exited"
    assert ev.latest_after(0) is not None and ev.latest_after(0).kind == "done"


def test_pre_delivery_crash_maps_to_crashed(tmp_path):
    sess, ev = _bare_session(tmp_path, DeadHandle(2))
    sess._run()
    assert sess.snapshot()["control_state"] == "terminal"  # was state="crashed"


def test_finish_is_idempotent(tmp_path):
    sess, ev = _bare_session(tmp_path, DeadHandle(0))
    sess._finish()
    n = ev.latest_seq()
    sess._finish()                        # second call must be a no-op
    assert ev.latest_seq() == n


# ---- observability: lifecycle logging (Task 6) -------------------------------

def _capture_logger():
    import io
    from daemon.obs import Logger
    buf = io.StringIO()
    return Logger(level="debug", stream=buf, audit_stream=buf), buf


def _events_in(buf):
    import json
    return [json.loads(l)["event"] for l in buf.getvalue().splitlines() if l.strip()]


def test_executor_spawned_logged_with_leader_fields(tmp_path):
    import json
    log, buf = _capture_logger()
    sess = Session("s1", "demo", ClaudeDriver(), None, Spec(), EventQueue(), logger=log)
    sess._handle = FakeHandle([], stop=sess._stop)
    sess._spawn_ts = 0.0
    sess._log_spawned(["runner", "-secret=kv/app"], "LocalLauncher")
    rec = [json.loads(l) for l in buf.getvalue().splitlines()
           if json.loads(l)["event"] == "executor_spawned"][0]
    assert rec["leader_pid"] == 4242 and rec["process_role"] == "pty_leader"
    assert "kv/app" not in json.dumps(rec["argv_redacted"])


def test_executor_exited_logged_once_on_pre_delivery_death(tmp_path):
    log, buf = _capture_logger()
    sess = Session("s1", "demo", ClaudeDriver(), None, Spec(), EventQueue(), logger=log)
    sess._handle = DeadHandle(2)
    sess._dialog = Dialog(tmp_path / "s1", tail_lines=Spec.tail_lines,
                          spool_max_bytes=Spec.spool_max_bytes)
    sess._spawn_ts = 0.0
    sess._run()
    assert _events_in(buf).count("executor_exited") == 1


def test_live_stop_logs_no_executor_exited(tmp_path):
    log, buf = _capture_logger()
    sess = Session("s1", "demo", ClaudeDriver(), None, Spec(), EventQueue(), logger=log)
    sess._handle = FakeHandle(["❯ "], stop=sess._stop)  # stays alive; one frame for the stop publish
    sess._dialog = Dialog(tmp_path / "s1", tail_lines=Spec.tail_lines,
                          spool_max_bytes=Spec.spool_max_bytes)
    sess._spawn_ts = 0.0
    sess._stop.set()                                     # operator stop, leader still alive
    sess._finish()
    assert "executor_exited" not in _events_in(buf)     # operator stop -> 'stopped', not exited


def test_executor_exited_logged_exactly_once_under_finalize_race(tmp_path):
    """Natural exit then a racing finalize (what stop() relies on) -> one executor_exited."""
    log, buf = _capture_logger()
    sess = Session("s1", "demo", ClaudeDriver(), None, Spec(), EventQueue(), logger=log)
    sess._handle = DeadHandle(0)
    sess._dialog = Dialog(tmp_path / "s1", tail_lines=Spec.tail_lines,
                          spool_max_bytes=Spec.spool_max_bytes)
    sess._spawn_ts = 0.0
    sess._run()                  # monitor finalizes -> one executor_exited
    sess._finish()               # racing second finalize -> _finalized guard, no second log
    assert _events_in(buf).count("executor_exited") == 1


def test_delivered_run_logs_readiness_and_delivery(tmp_path):
    """Successful delivery path emits the DEBUG/INFO lifecycle rows."""
    box = "Welcome back!\n❯ \n⏵⏵ ask mode (shift+tab to cycle)\n"
    sess, handle, _ = make_session(tmp_path, frames=[box])
    log, buf = _capture_logger()
    sess._log = log
    sess.start("create report.md", str(tmp_path))
    _wait_for(lambda: sess._task_delivery == "delivered")
    sess.stop()
    evs = _events_in(buf)
    assert "executor_spawned" in evs and "cli_ready" in evs
    assert "delivery_attempt" in evs and "delivery_confirmed" in evs


def test_run_loop_survives_hook_flow_with_real_logger(tmp_path):
    # nelix-6qs: close the "crashes only with a real Logger" class. The REAL monitor
    # (_run -> _loop -> _drain_hooks) runs with a real Logger in prod, and _drain_hooks logs a
    # `hook_applied` DEBUG line per drained hook. A bad self._log.*(...) call (the event= kwarg
    # collision of 74c162e, or any future one) raises TypeError on the FIRST hook and kills the
    # monitor — invisible when the loop wires logger=None (the line sits behind
    # `if self._log is not None:`). Drive the whole real wiring with a real Logger and a real hook
    # flow so a re-landing of that class FAILS here instead of shipping green.
    box = "Welcome back!\n❯ \n⏵⏵ ask mode (shift+tab to cycle)\n"
    sess, handle, _ = make_session(tmp_path, frames=[box])
    log, buf = _capture_logger()
    sess._log = log
    sess.start("do the thing", str(tmp_path))
    assert _wait_for(lambda: sess._task_delivery == "delivered")
    sess.on_hook(HookEvent("s1", "UserPromptSubmit"))          # first hook: hooks take over (busy)
    sess.on_hook(HookEvent("s1", "Stop"))                      # -> idle
    reached_idle = _wait_for(lambda: sess.snapshot()["control_state"] == "idle")
    sess.stop()
    evs = _events_in(buf)
    assert reached_idle                                         # the Stop hook drained (monitor alive)
    assert "hook_applied" in evs                                # the crashing line actually fired
    assert "monitor_exception" not in evs                      # the monitor survived the hook flow


# ---- delivery must not wedge on a blocking PTY write (real PtySession) --------

_RAW_IGNORE_STDIN_CHILD = (
    "import sys,tty,time\n"
    "try:\n"
    "    tty.setraw(sys.stdin.fileno())\n"     # raw mode (no echo), like a real TUI
    "except Exception:\n"
    "    pass\n"
    "sys.stdout.write('Welcome\\r\\n\\u276f \\r\\n\\u23f5\\u23f5 ask mode (shift+tab to cycle)\\r\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(60)\n"                          # NEVER reads stdin -> our write would block
)


def test_delivery_does_not_wedge_when_executor_ignores_stdin(tmp_path, monkeypatch):
    """Regression for the production hang: a real executor that renders its prompt but
    never drains stdin. Delivering a task larger than the PTY buffer must NOT block the
    monitor thread forever in os.write — it must resolve (delivery_failed + finalize)
    within a bounded time. FAILS on the pre-fix blocking-write code (monitor wedges)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from daemon.pty_session import PtySession
    from daemon.broker_client import BrokerClient

    child = ["python3", "-c", _RAW_IGNORE_STDIN_CHILD]
    broker = BrokerClient()                          # real spawn path: PTY fork happens in the broker

    class _Launcher:
        def start(self, spec, cwd, cols, rows, dialog=None, transcript=None, **_kw):
            master, pid, pgid = broker.spawn(child, cwd, dict(os.environ), cols, rows)
            return PtySession(master, pid, pgid, cols=cols, rows=rows,
                              dialog=dialog, transcript=transcript)
        def stop(self, h):
            h.close()

    sess = Session("s1", "dummy", ClaudeDriver(), _Launcher(), FastConfirmSpec(), EventQueue())
    sess.start("X" * 65536, str(tmp_path))      # task far larger than the PTY input buffer
    try:
        assert _wait_for(lambda: sess._finalized, timeout=8.0), \
            "monitor wedged on the blocking PTY write (delivery never resolved)"
        assert sess._task_delivery == "failed"
    finally:
        # bulletproof cleanup independent of teardown robustness (Fix B): kill the whole
        # process group so a wedged write unblocks immediately, then stop normally.
        import os as _os, signal as _signal
        try:
            _os.killpg(_os.getpgid(sess._handle.leader_pid()), _signal.SIGKILL)
        except Exception:
            pass
        sess.stop()
        broker.close()


# ---- delivery must drain the executor's output while writing (real PtySession) ----

# An executor that DOES read stdin but echoes every chunk (amplified), so its render output
# fills the PTY output buffer. If the monitor writes the task without draining that output,
# the child blocks on its own os.write, stops reading stdin, the input buffer fills, and the
# bounded write deadlocks -> write_unconfirmed. Mirrors Claude/zai collapsing a large paste.
_RAW_ECHO_FLOOD_CHILD = (
    "import sys,os,tty,time\n"
    "fd=0\n"
    "try:\n"
    "    tty.setraw(fd)\n"
    "except Exception:\n"
    "    pass\n"
    "os.write(1,'\\u276f \\r\\n\\u23f5\\u23f5 ask mode (shift+tab to cycle)\\r\\n'.encode())\n"
    "buf=b''\n"
    "while True:\n"
    "    ch=os.read(fd,1024)\n"
    "    if not ch:\n"
    "        break\n"
    "    os.write(1,ch)\n"                          # echo (advances the rendered prompt)
    "    os.write(1,ch)\n"                          # amplify: fill the output buffer faster than input drains
    "    buf+=ch\n"
    # Like a real bracketed-paste CLI: once the whole paste arrives, COLLAPSE it to a placeholder on
    # the prompt line (clear + redraw) so the prompt stays visible and delivery can confirm via the
    # active input region (the verbatim echo otherwise scrolls the prompt off-screen).
    "    if b'\\x1b[201~' in buf:\n"
    "        os.write(1,'\\x1b[2J\\x1b[H\\u276f [Pasted text #1]\\r\\n\\u23f5\\u23f5 ask mode (shift+tab to cycle)\\r\\n'.encode())\n"
    "        break\n"
    "    if b'\\r' in ch:\n"                        # the submit key (sent only after confirmation)
    "        break\n"
    "time.sleep(30)\n"
)


class FloodConfirmSpec(Spec):
    delivery_confirm_seconds = 5.0      # generous budget: the drained write must finish well inside it


def test_delivery_drains_output_so_large_task_reaches_executor(tmp_path, monkeypatch):
    """A real executor that READS stdin but emits output per chunk (filling the PTY output
    buffer). Delivering a task larger than the PTY buffers must still reach it (delivered),
    which is only possible if the monitor DRAINS the child's output while writing — otherwise
    both sides deadlock and the bounded write fails with write_unconfirmed. FAILS on the
    pre-fix write() that waits only for writability and never reads during the write."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from daemon.pty_session import PtySession
    from daemon.broker_client import BrokerClient

    child = ["python3", "-c", _RAW_ECHO_FLOOD_CHILD]
    broker = BrokerClient()                          # real spawn path: PTY fork happens in the broker

    class _Launcher:
        def start(self, spec, cwd, cols, rows, dialog=None, transcript=None, **_kw):
            master, pid, pgid = broker.spawn(child, cwd, dict(os.environ), cols, rows)
            return PtySession(master, pid, pgid, cols=cols, rows=rows,
                              dialog=dialog, transcript=transcript)
        def stop(self, h):
            h.close()

    sess = Session("s1", "dummy", ClaudeDriver(), _Launcher(), FloodConfirmSpec(), EventQueue())
    sess.start("X" * 65536, str(tmp_path))          # task far larger than the PTY buffers
    try:
        assert _wait_for(lambda: sess._task_delivery == "delivered", timeout=12.0), \
            (f"large task never reached the executor (delivery={sess._task_delivery}); "
             "the monitor did not drain PTY output during the write -> flow-control deadlock")
    finally:
        import os as _os, signal as _signal
        try:
            _os.killpg(_os.getpgid(sess._handle.leader_pid()), _signal.SIGKILL)
        except Exception:
            pass
        sess.stop()
        broker.close()


def test_start_writes_child_record(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib, paths
    importlib.reload(paths)
    from daemon import reaper
    from daemon.session import Session

    class _Insp:
        def start_fingerprint(self, pid): return f"fp-{pid}"
        def is_alive(self, pid): return False   # needed by kill_group in _finish_cleanup
    class _Killer:
        def killpg(self, pgid, sig): pass

    # _finish_cleanup now calls forget_child after start(); patch it to a no-op so the
    # assertion below doesn't race with the monitor thread deleting the record.
    monkeypatch.setattr(reaper, "forget_child", lambda p: None)

    ev = EventQueue()
    sess = Session("s-deadbeef", "demo", ClaudeDriver(), _NoopLauncher(FakeHandle(["ready"])),
                   Spec(), ev)
    sess.reaper_ctx = reaper.ReaperContext(daemon_pid=10, daemon_fingerprint="d1", grace=0.05,
                                           inspector=_Insp(), killer=_Killer())
    sess._stop.set()                                   # don't run the monitor loop in this test
    sess.start("hello", str(tmp_path))
    rec = reaper.read_child(paths.sessions_root() / "s-deadbeef")
    assert rec["sid"] == "s-deadbeef"
    assert rec["pid"] == 4242 and rec["pgid"] == 4242
    assert rec["daemon_pid"] == 10 and rec["daemon_fingerprint"] == "d1"
    assert rec["child_fingerprint"] == "fp-4242"
    sess.stop()


def test_finish_frees_slot_and_forgets_record_on_clean_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib, paths
    importlib.reload(paths)
    from daemon import reaper
    from daemon.session import Session

    class _Insp:
        def start_fingerprint(self, pid): return f"fp-{pid}"
        def is_alive(self, pid): return False
    class _Killer:
        def __init__(self): self.calls = []
        def killpg(self, pgid, sig): self.calls.append((pgid, sig))

    freed = []
    ev = EventQueue()
    sess = Session("s-cafef00d", "demo", ClaudeDriver(), _NoopLauncher(DeadHandle(0)), Spec(), ev)
    sess.on_terminal = freed.append
    sess.reaper_ctx = reaper.ReaperContext(10, "d1", 0.05, _Insp(), _Killer())
    sess.start("hi", str(tmp_path))
    sess._thread.join(timeout=5)
    assert freed == ["s-cafef00d"]                       # slot freed
    assert reaper.read_child(paths.sessions_root() / "s-cafef00d") is None   # record forgotten


def test_finish_kills_group_when_monitor_dies_with_child_alive(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib, paths, signal
    importlib.reload(paths)
    from daemon import reaper
    from daemon.session import Session

    class _Insp:
        def start_fingerprint(self, pid): return f"fp-{pid}"
        def is_alive(self, pid): return True            # child stays alive
    killer_calls = []
    class _Killer:
        def killpg(self, pgid, sig): killer_calls.append((pgid, sig))

    freed = []
    ev = EventQueue()
    sess = Session("s-0badf00d", "demo", ClaudeDriver(), _NoopLauncher(FakeHandle(["x"])), Spec(), ev)
    sess.on_terminal = freed.append
    sess.reaper_ctx = reaper.ReaperContext(10, "d1", 0.02, _Insp(), _Killer())
    # force the monitor body to raise so _finish hits the monitor-dead branch with child alive
    monkeypatch.setattr(sess, "_wait_until_ready",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    sess.start("hi", str(tmp_path))
    sess._thread.join(timeout=5)
    assert (4242, signal.SIGTERM) in killer_calls        # group killed despite child "alive"
    assert freed == ["s-0badf00d"]


def test_respond_after_terminal_is_rejected_without_writing(tmp_path):
    from daemon.session import Session, RespondOutcome
    ev = EventQueue()
    h = FakeHandle(["x"])
    sess = Session("s-11112222", "demo", ClaudeDriver(), _NoopLauncher(h), Spec(), ev)
    sess._handle = h
    sess._decision = {"kind": "waiting_for_user", "event_id": "e1", "decision_id": "d1",
                      "seq": 1, "text": "", "hint": None, "hung": False}
    sess._closing = True                                  # terminal cleanup has started
    out = sess.respond("answer")
    assert out.status == "terminal"
    assert h.writes == []                                 # nothing typed into a closing PTY


def test_start_asserts_group_leader_when_reaping(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib, paths, pytest
    importlib.reload(paths)
    from daemon import reaper
    from daemon.session import Session

    class _BadHandle(FakeHandle):
        def leader_pid(self): return 100
        def leader_pgid(self): return 200     # pid != pgid -> not its own group leader

    class _Insp:
        def start_fingerprint(self, pid): return "fp"
    class _Killer:
        def killpg(self, pgid, sig): pass

    ev = EventQueue()
    sess = Session("s-badleader", "demo", ClaudeDriver(), _NoopLauncher(_BadHandle(["x"])), Spec(), ev)
    sess.reaper_ctx = reaper.ReaperContext(10, "d1", 0.05, _Insp(), _Killer())
    sess._stop.set()
    with pytest.raises(RuntimeError):
        sess.start("hi", str(tmp_path))


def test_snapshot_includes_task_delivery(tmp_path):
    sess, _ = _session(tmp_path)
    sess._task_delivery = "pending"
    assert sess.snapshot()["task_delivery"] == "pending"


def test_terminal_snapshot_includes_task_delivery(tmp_path):
    sess, _ = _session(tmp_path)
    sess._terminal_kind = "stopped"
    sess._task_delivery = "delivered"
    assert sess.terminal_snapshot()["task_delivery"] == "delivered"
