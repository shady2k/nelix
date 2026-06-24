import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths                                   # noqa: E402
from daemon.session import Session            # noqa: E402
from daemon.dialog import Dialog              # noqa: E402
from daemon.drivers.claude import ClaudeDriver  # noqa: E402
from daemon.events import EventQueue          # noqa: E402


class Spec:
    settle_seconds = 1.5
    hang_timeout = 600.0
    tail_lines = 100
    status_tail_chars = 4000
    dialog_page_chars = 8000
    spool_max_bytes = 1_000_000


class HangSpec(Spec):
    hang_timeout = 5.0


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
    return sess, ev


def test_sessions_dir_resolves_under_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sess = Session("s1", "demo", ClaudeDriver(), None, Spec(), EventQueue())
    assert sess._sessions_dir == paths.sessions_root()


def test_stop_edge_emits_frozen_respondable_event(monkeypatch, tmp_path):
    frames = ["thinking… esc to interrupt", "Here is my answer.\n❯ ",
              "Here is my answer.\n❯ ", "Here is my answer.\n❯ "]
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
    box = "Hello answer.\n❯ "
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


def test_hang_writes_esc_and_emits_hung(monkeypatch, tmp_path):
    sess, ev = _session(tmp_path, ["working… esc to interrupt"] * 3, spec=HangSpec())
    monkeypatch.setattr("daemon.session.time.time", _clock([0, 0, 10]))
    sess._loop()
    assert "\x1b" in sess._handle.writes                  # ESC written once
    pend = ev.pending("s1")
    assert pend is not None and pend.hung is True


def test_respond_answers_and_advances_turn(monkeypatch, tmp_path):
    monkeypatch.setattr("daemon.session.time.sleep", lambda *_: None)
    box = "answer\n❯ "
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
    monkeypatch.setattr(sess, "_wait_until_ready", lambda *a, **k: None)
    monkeypatch.setattr(sess, "_ensure_ask_mode", lambda *a, **k: None)
    monkeypatch.setattr(sess, "_submit", lambda *a, **k: None)
    monkeypatch.setattr(sess, "_loop", lambda *a, **k: None)
    sess.start("do it", cwd="/work/repo")
    sess._stop.set()
    assert seen["cwd"] == "/work/repo"


def test_ensure_ask_mode_writes_driver_toggle(monkeypatch, tmp_path):
    monkeypatch.setattr("daemon.session.time.sleep", lambda *_: None)
    sess, _ = _session(tmp_path, ["normal mode, no askmode marker"])
    sess._ensure_ask_mode(attempts=2)
    assert sess._driver.ask_mode_toggle in "".join(sess._handle.writes)
