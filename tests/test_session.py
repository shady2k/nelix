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
    settle_seconds = 1.5
    max_idle_seconds = 600.0
    tail_lines = 100
    status_tail_chars = 4000
    dialog_page_chars = 8000
    spool_max_bytes = 1_000_000


class HangSpec(Spec):
    max_idle_seconds = 5.0


class BackstopSpec(Spec):
    max_idle_seconds = 0.2          # fast no-progress backstop for real-thread tests


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

    def write(self, data):
        self.writes.append(data)

    def flush_viewport(self, dialog):
        for ln in self.render().splitlines():
            t = ln.rstrip()
            if t:
                dialog.add_line(t)

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

    def write(self, data):
        self.writes.append(data)

    def flush_viewport(self, dialog):
        pass

    def close(self):
        pass


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
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0]))
    sess._loop()
    assert ev.pending("s1") is None                       # 'done' is not respondable
    last = ev.latest_after(0)
    assert last is not None and last.kind == "done"
    assert sess.snapshot()["state"] == "exited"


def test_exit_nonzero_emits_crashed(monkeypatch, tmp_path):
    sess, ev = _session(tmp_path, handle=DeadHandle(2))
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0]))
    sess._loop()
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
    eid = sess.snapshot()["decision"]["event_id"]
    assert sess._dialog.current_turn() == 0
    assert sess.respond(eid, "1") is True
    assert ev.pending("s1") is None                       # answered
    assert sess._dialog.current_turn() == 1               # new turn boundary
    assert sess.snapshot().get("decision") is None        # cleared
    assert "\r" in sess._handle.writes and any("1" in w for w in sess._handle.writes)


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

    def write(self, data):
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

    def close(self):
        pass


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


def test_blocked_no_echo_emits_blocked_unknown(tmp_path):
    # An apparent input box where the typed task never echoes -> blocked(unknown), no Enter.
    class NoEchoHandle(LiveHandle):
        def write(self, data):
            self.writes.append(data)            # record but do NOT echo into the frame

    box = "Welcome back!\n❯ \n⏵⏵ ask mode (shift+tab to cycle)\n"
    sess, handle, _ = make_session(tmp_path, frames=[box], handle_cls=NoEchoHandle)
    sess.start("create report.md", str(tmp_path))
    _wait_for(lambda: sess._decision and sess._decision["kind"] == "blocked"
              and sess._decision.get("hint") == "unknown", timeout=5)
    assert sess._decision["hint"] == "unknown"
    assert sess._task_delivery == "pending"
    assert "\r" not in handle.writes               # never pressed Enter without echo
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
    ev = sess._decision["event_id"]
    assert sess.respond(ev, "1") is True
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
    eid = sess._decision["event_id"]
    assert sess.respond(eid, "1") is True
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


def test_session_screen_cleans_by_default_and_raw_is_exact(tmp_path):
    raw = "╭──────╮\n│ hi  │\n╰──────╯\n"
    sess, handle, _ = make_session(tmp_path, frames=[raw])
    sess._handle = handle                  # render() serves the framed frame
    from daemon.session import _clean_screen
    assert sess.screen() == _clean_screen(raw)
    assert sess.screen(raw=True) == raw    # exact untouched render()
    assert "│" in sess.screen(raw=True) and "│" not in sess.screen()
