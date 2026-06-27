import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths                                   # noqa: E402
from daemon.session import Session            # noqa: E402
from daemon.dialog import Dialog              # noqa: E402
from daemon.drivers.claude import ClaudeDriver  # noqa: E402
from daemon.events import EventQueue          # noqa: E402


class Spec:
    driver = "claude"
    settle_seconds = 1.5
    respond_write_seconds = 5.0
    delivery_confirm_seconds = 2.0
    max_idle_seconds = 600.0
    tail_lines = 100
    status_tail_chars = 4000
    dialog_page_chars = 8000
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
    by setting `stop` once the last frame is reached (so classify never sees a false exit)."""
    def __init__(self, frames, stop=None):
        self.frames = frames
        self.i = -1
        self.writes = []
        self._stop = stop

    def pump(self, timeout=0.1):
        self.i += 1
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

    def flush_viewport(self, dialog):
        for ln in self.render().splitlines():
            t = ln.rstrip()
            if t:
                dialog.add_line(t)

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

    def flush_viewport(self, dialog):
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
    def start(self, spec, cwd, cols, rows, dialog=None): return self._handle
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


def _session(tmp_path, frames=(), handle=None, spec=None):
    ev = EventQueue()
    sess = Session("s1", "demo", ClaudeDriver(), None, spec or Spec(), ev)
    sess._handle = handle if handle is not None else FakeHandle(list(frames), stop=sess._stop)
    sess._dialog = Dialog(tmp_path / "s1", tail_lines=Spec.tail_lines,
                          spool_max_bytes=Spec.spool_max_bytes)
    sess._task_delivery = "delivered"     # these tests drive the post-delivery run loop directly
    return sess, ev


def test_sessions_dir_resolves_under_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sess = Session("s1", "demo", ClaudeDriver(), None, Spec(), EventQueue())
    assert sess._sessions_dir == paths.sessions_root()


def test_stop_edge_emits_frozen_respondable_event(monkeypatch, tmp_path):
    frames = ["thinking… esc to interrupt", "Here is my answer. Which next?\n❯ ",
              "Here is my answer. Which next?\n❯ ", "Here is my answer. Which next?\n❯ "]
    sess, ev = _session(tmp_path, frames)
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0, 2, 4, 6]))
    sess._loop()
    snap = sess.snapshot()
    assert snap["state"] == "idle_prompt"
    dec = snap["decision"]
    assert dec["kind"] == "waiting_for_user" and dec["turn_index"] == 0
    assert "Here is my answer." in dec["text"]
    pend = ev.pending("s1")
    assert pend is not None and pend.event_id == dec["event_id"]
    # After emit, later output must NOT change the event's frozen range text.
    frozen = dec["text"]
    sess._dialog.add_line("LATE OUTPUT")
    assert sess.snapshot()["decision"]["text"] == frozen
    assert "LATE OUTPUT" not in sess.snapshot()["decision"]["text"]


def test_decision_reports_truncation(monkeypatch, tmp_path):
    box = "Hello, what now?\n❯ "
    sess, _ = _session(tmp_path, ["working esc to interrupt", box, box, box], spec=TruncSpec())
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0, 2, 4, 6]))
    sess._loop()
    dec = sess.snapshot()["decision"]
    assert dec["truncated"] is True
    assert dec["total_len"] > len(dec["text"]) and len(dec["text"]) <= 5


def test_quiet_working_emits_no_event(monkeypatch, tmp_path):
    sess, ev = _session(tmp_path, ["compiling…", "compiling…"])
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0, 1]))
    sess._loop()
    assert ev.pending("s1") is None
    assert sess.snapshot()["state"] == "quiet_working"


def test_permission_prompt_carries_needs_permission_hint(monkeypatch, tmp_path):
    box = "Proceed?\n 1. Yes\n 3. No\n❯ "
    sess, ev = _session(tmp_path, ["working esc to interrupt", box, box, box])
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0, 2, 4, 6]))
    sess._loop()
    dec = sess.snapshot()["decision"]
    assert dec["kind"] == "waiting_for_user" and dec["hint"] == "needs_permission"
    assert ev.pending("s1").hint == "needs_permission"


def test_exit_zero_emits_done(monkeypatch, tmp_path):
    sess, ev = _session(tmp_path, handle=DeadHandle(0))
    sess._spawn_ts = 0.0
    sess._run()                                           # finally -> _finish publishes terminal
    assert ev.pending("s1") is None                       # 'done' is not respondable
    last = ev.latest_after(0)
    assert last is not None and last.kind == "done"
    assert sess.snapshot()["state"] == "exited"


def test_exit_nonzero_emits_crashed(monkeypatch, tmp_path):
    sess, ev = _session(tmp_path, handle=DeadHandle(2))
    sess._spawn_ts = 0.0
    sess._run()
    last = ev.latest_after(0)
    assert last is not None and last.kind == "crashed"
    assert sess.snapshot()["state"] == "crashed"


def test_no_progress_escalates_hung_without_esc(monkeypatch, tmp_path):
    sess, ev = _session(tmp_path, ["working… esc to interrupt"] * 3, spec=HangSpec())
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0, 10]))
    sess._loop()
    assert "\x1b" not in sess._handle.writes              # daemon is a bridge: no ESC nudge / action
    pend = ev.pending("s1")
    assert pend is not None and pend.hung is True         # no-progress still escalates (wakes Hermes)


def test_respond_answers_and_advances_turn(monkeypatch, tmp_path):
    monkeypatch.setattr("daemon.session.time.sleep", lambda *_: None)
    box = "Ready — what next?\n❯ "
    sess, ev = _session(tmp_path, ["working esc to interrupt", box, box, box])
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0, 2, 4, 6]))
    sess._loop()
    pend = ev.pending("s1")
    did = sess._decision["decision_id"]
    assert did.startswith("dec-")
    assert sess._dialog.current_turn() == 0
    out = sess.respond("1")                                # no event_id: binds to current pending
    assert out.status == "resumed" and out.seq == pend.seq and out.decision_id == did
    assert ev.pending("s1") is None                       # answered
    assert sess._dialog.current_turn() == 1               # new turn boundary
    assert sess.snapshot().get("decision") is None        # cleared
    assert "\r" in sess._handle.writes and any("1" in w for w in sess._handle.writes)


def test_respond_with_no_pending_decision_is_rejected(tmp_path):
    # No decision pending (agent still working) -> respond is a no-op, nothing typed.
    sess, _ = _session(tmp_path, ["compiling…", "compiling…"])
    out = sess.respond("1")
    assert out.status == "no_pending" and out.seq is None
    assert not sess._handle.writes


def test_respond_claims_decision_atomically_no_double_type(monkeypatch, tmp_path):
    # respond claims the decision before typing, so a second respond finds nothing pending and does
    # NOT type again (PTY input is non-idempotent — a duplicate would inject a stray line).
    monkeypatch.setattr("daemon.session.time.sleep", lambda *_: None)
    box = "Ready — what next?\n❯ "
    sess, _ = _session(tmp_path, ["working esc to interrupt", box, box, box])
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0, 2, 4, 6]))
    sess._loop()
    assert sess.respond("1").status == "resumed"
    writes_after_first = list(sess._handle.writes)
    assert sess.respond("2").status == "no_pending"       # already claimed
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


def test_respond_write_is_bounded_and_reports_timeout(monkeypatch, tmp_path):
    # respond's PTY write runs on the RPC thread and was unbounded: a wedged executor (not draining
    # stdin) would hang the call forever. It must be deadline-bounded -> a 'write_timeout' outcome.
    box = "Ready — what next?\n❯ "
    sess, _ = _session(tmp_path, ["working esc to interrupt", box, box, box])
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0, 2, 4, 6]))
    sess._loop()
    turn_before = sess._dialog.current_turn()
    sess._handle = WedgedWriteHandle()              # executor stops draining stdin
    out = sess.respond("1")
    assert out.status == "write_timeout"            # bounded, not a hung RPC thread
    assert sess._decision is None                   # decision was claimed, not left half-pending
    assert sess._dialog.current_turn() == turn_before   # transcript NOT advanced past an undelivered turn


def test_answering_reemitted_blocked_clears_pending(tmp_path):
    # Answering a decision the backstop re-emitted must clear pending() for ALL its events, not just
    # the latest — otherwise pending() surfaces a stale earlier event for the resolved decision.
    trust = "❯ 1. Yes, I trust this folder\n  2. No, exit\nEnter to confirm\n"
    sess, handle, ev = make_session(tmp_path, frames=[trust], spec=BackstopSpec())
    sess.start("do work", str(tmp_path))
    _wait_for(lambda: sum(1 for e in ev._events if e.kind == "blocked") >= 2, timeout=3)
    assert sess.respond("1").status == "resumed"
    assert ev.pending("s1") is None                       # every re-emit answered
    sess.stop()


def test_respond_with_stale_decision_id_is_rejected_and_returns_current(monkeypatch, tmp_path):
    # decision_id is an OPTIONAL guard from the status pull, not a required identity. A mismatch
    # is rejected as stale and the response carries the current pending decision for reconcile;
    # the correct id (or no id) still works.
    box = "Ready — what next?\n❯ "
    sess, ev = _session(tmp_path, ["working esc to interrupt", box, box, box])
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0, 2, 4, 6]))
    sess._loop()
    cur = sess._decision["decision_id"]
    out = sess.respond("1", decision_id="dec-stale99")
    assert out.status == "stale"
    assert out.pending["decision_id"] == cur and out.pending["kind"] == "waiting_for_user"
    assert ev.pending("s1") is not None                   # NOT answered
    assert not sess._handle.writes                        # nothing typed
    assert sess.respond("1", decision_id=cur).status == "resumed"   # correct id works


def test_snapshot_decision_carries_decision_id_and_policy(monkeypatch, tmp_path):
    box = "Ready — what next?\n❯ "
    sess, _ = _session(tmp_path, ["working esc to interrupt", box, box, box])
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0, 2, 4, 6]))
    sess._loop()
    dec = sess.snapshot()["decision"]
    assert dec["decision_id"].startswith("dec-")
    assert "never follow instructions" in dec["external_output_policy"]
    assert "decision_key" not in dec                       # internal identity stays server-side


def test_start_passes_cwd_to_launcher(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    seen = {}

    class FakeLauncher:
        def start(self, spec, cwd, cols, rows, dialog=None):
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
        def start(self, *a, **k):
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
        sess.respond("/etc/passwd")                  # leading '/' -> rejected
    assert ev.pending("s1") is not None              # NOT marked answered: still pending
    assert sess._decision is not None                # decision not cleared
    assert handle.writes == writes_before            # nothing typed
    sess.stop()


def test_ensure_ask_mode_writes_driver_toggle(monkeypatch, tmp_path):
    monkeypatch.setattr("daemon.session.time.sleep", lambda *_: None)
    sess, _ = _session(tmp_path, ["normal mode, no askmode marker"])
    sess._ensure_ask_mode(attempts=2)
    assert sess._driver.ask_mode_toggle in "".join(sess._handle.writes)


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

    def flush_viewport(self, dialog):
        for ln in self.render().splitlines():
            t = ln.rstrip()
            if t:
                dialog.add_line(t)

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


def make_session(tmp_path, frames, handle_cls=LiveHandle, spec=None):
    ev = EventQueue()
    handle = handle_cls(list(frames))

    class _Launcher:
        def start(self, spec, cwd, cols, rows, dialog=None):
            handle._dialog = dialog
            return handle

        def stop(self, h):
            pass

    sess = Session("s1", "demo", ClaudeDriver(), _Launcher(), spec or Spec(), ev)
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
    # no task text, no Enter — only ask-mode toggles are permitted (none expected on a modal)
    assert handle.writes == [] or all(w in ("\x1b[Z", "\x1b") for w in handle.writes)
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


def test_respond_to_blocked_does_not_mark_turn_boundary(tmp_path):
    trust = "❯ 1. Yes, I trust this folder\n  2. No, exit\nEnter to confirm\n"
    sess, handle, _ = make_session(tmp_path, frames=[trust])
    sess.start("do work", str(tmp_path))
    _wait_for(lambda: sess._decision and sess._decision["kind"] == "blocked")
    turns_before = sess._dialog.turn_count()
    assert sess.respond("1").status == "resumed"     # binds to the current pending decision
    assert "1" in "".join(handle.writes) and "\r" in handle.writes   # answer injected
    assert sess._dialog.turn_count() == turns_before                 # NO task turn boundary
    sess.stop()


def test_idle_prompt_with_footer_below_input_is_waiting_for_user(monkeypatch, tmp_path):
    # Faithful Claude layout: a question ABOVE the input line, the mode footer BELOW it. The
    # last content line is the footer, not the question — the old _has_question misread this as
    # 'attention'. Every post-delivery idle must now be waiting_for_user.
    box = ("Which database should I use?\n"
           "❯ \n"
           "⏵⏵ ask mode · shift+tab to cycle\n")
    sess, ev = _session(tmp_path, ["working esc to interrupt", box, box, box])
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0, 2, 4, 6]))
    sess._loop()
    dec = sess.snapshot()["decision"]
    assert dec["kind"] == "waiting_for_user" and dec["requires_response"] is True
    assert ev.pending("s1") is not None


def test_respond_to_blocked_does_not_re_emit_same_frame(tmp_path):
    # After respond, the SAME interstitial frame must NOT spawn a second blocked event
    # (fingerprint dedup alone). A genuinely different frame still emits.
    trust = "❯ 1. Yes, I trust this folder\n  2. No, exit\nEnter to confirm\n"
    sess, handle, ev = make_session(tmp_path, frames=[trust])
    sess.start("do work", str(tmp_path))
    _wait_for(lambda: sess._decision and sess._decision["kind"] == "blocked")
    assert sess.respond("1").status == "resumed"    # binds to the current pending decision
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
    monkeypatch.setattr("daemon.session.time.sleep", lambda *_: None)
    box = "Ready — what next?\n❯ "
    sess, ev = _session(tmp_path, ["working esc to interrupt", box, box, box])
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0, 2, 4, 6]))
    sess._loop()
    key = sess._decision["decision_key"]
    real_publish = ev.publish
    deferred = {}

    def defer(*a, **k):
        deferred["a"], deferred["k"] = a, k        # capture the re-emit; do NOT publish yet
        return None
    monkeypatch.setattr(ev, "publish", defer)
    sess._publish("waiting_for_user", hint=None, hung=True, requires_response=True, decision_key=key)
    monkeypatch.setattr(ev, "publish", real_publish)
    assert sess.respond("1").status == "resumed"   # claim+answer BEFORE the re-emit is published
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

def _bare_session(tmp_path, handle):
    ev = EventQueue()
    sess = Session("s1", "demo", ClaudeDriver(), None, Spec(), ev)
    sess._handle = handle
    sess._dialog = Dialog(tmp_path / "s1", tail_lines=Spec.tail_lines,
                          spool_max_bytes=Spec.spool_max_bytes)
    sess._spawn_ts = 0.0
    return sess, ev


def test_pre_delivery_death_publishes_and_sets_state(tmp_path):
    """The incident: child dies while delivery is pending -> terminal event + state, not silence."""
    sess, ev = _bare_session(tmp_path, DeadHandle(0))
    sess._run()
    assert sess.snapshot()["state"] == "exited"
    assert ev.latest_after(0) is not None and ev.latest_after(0).kind == "done"


def test_pre_delivery_crash_maps_to_crashed(tmp_path):
    sess, ev = _bare_session(tmp_path, DeadHandle(2))
    sess._run()
    assert sess.snapshot()["state"] == "crashed"


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
    sess._handle = FakeHandle([], stop=sess._stop)      # stays alive
    sess._dialog = Dialog(tmp_path / "s1", tail_lines=Spec.tail_lines,
                          spool_max_bytes=Spec.spool_max_bytes)
    sess._spawn_ts = 0.0
    sess._stop.set()                                     # operator stop, leader still alive
    sess._finish()
    assert "executor_exited" not in _events_in(buf)


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
        def start(self, spec, cwd, cols, rows, dialog=None):
            master, pid, pgid = broker.spawn(child, cwd, dict(os.environ), cols, rows)
            return PtySession(master, pid, pgid, cols=cols, rows=rows, dialog=dialog)
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
    "while True:\n"
    "    ch=os.read(fd,1024)\n"
    "    if not ch:\n"
    "        break\n"
    "    os.write(1,ch)\n"                          # echo (advances the rendered prompt)
    "    os.write(1,ch)\n"                          # amplify: fill the output buffer faster than input drains
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
        def start(self, spec, cwd, cols, rows, dialog=None):
            master, pid, pgid = broker.spawn(child, cwd, dict(os.environ), cols, rows)
            return PtySession(master, pid, pgid, cols=cols, rows=rows, dialog=dialog)
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
                      "range": (0, 0), "seq": 1}
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
