"""Real-capture startup no-output backstop (fix/startup-no-output-bound).

An executor whose PTY renders NOTHING classifiable — e.g. a launcher that kept the terminal
foreground process group so the leaf CLI is SIGTTIN-stopped (0 bytes, blank screen forever) —
leaves prompt_kind stuck at "none", so the blocked-gated max_idle nag is unreachable and the
monitor's pre-delivery loop spins forever. Session._run must bound the pre-delivery/startup phase
with an UNCONDITIONAL deadline on the INJECTED clock: past startup_timeout_seconds with nothing
classifiable ever shown, terminal-fail SYMMETRICALLY with crash/exit — one SURFACED escalation
(delivery_failed / hint=startup_no_output), a forensic lifecycle record, and a clean reap/slot-free.
It is avoided ONLY by a classifiable prompt (delivery flips task_delivery out of "pending") or by a
modal/permission that routes through _emit_blocked (the _blocked_fp exclusion); a stable but
NON-classifiable banner does NOT exempt it — waiting to inject the task is bounded unconditionally
(nelix-b5q).

Frames are produced by feeding synthetic/real PTY bytes through the REAL GhosttyRenderer (the repo's
real-capture norm — never hand-fabricated frame strings), then driven through the REAL Session via
tests/_session_replay's inline delivery harness with a FakeClock the test advances deterministically.
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.obs import Logger                                     # noqa: E402
from daemon.renderer.ghostty import GhosttyRenderer               # noqa: E402
from tests._session_replay import delivery_run, raw_frames, Spec  # noqa: E402


def _render(chunks, *, cols=120, rows=40):
    """Feed each byte chunk through the REAL renderer, collecting the rendered frame after each — the
    repo's synthetic-bytes-through-the-real-renderer capture path. Returns list[str]."""
    r = GhosttyRenderer(cols, rows)
    out = []
    try:
        for ch in chunks:
            r.feed(ch)
            out.append(r.render())
    finally:
        r.close()
    return out


def _blank_frame(cols=120, rows=40):
    r = GhosttyRenderer(cols, rows)
    try:
        return r.render()                    # the true render of a PTY that emitted 0 bytes
    finally:
        r.close()


class _FastStartupSpec(Spec):
    startup_timeout_seconds = 3.0            # small deadline so the FakeClock crosses it in a few pumps


def _startup_records(buf):
    recs = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    return [r for r in recs if r.get("event") == "startup_no_output"]


def _has_event(ev, kind, hint=None):
    return any(e.kind == kind and (hint is None or e.hint == hint) for e in ev._events)


# ---- 1. empty screen (0 bytes): terminal fail + surfaced escalation + forensic record + reap ------

def test_blank_screen_startup_deadline_fails_terminally(tmp_path):
    buf = io.StringIO()
    freed = []
    sess, ev, handle = delivery_run(
        tmp_path, [_blank_frame()] * 80, task="do the work", spec=_FastStartupSpec(),
        logger=Logger(level="debug", stream=buf), on_terminal=lambda sid: freed.append(sid))

    # terminal fail, symmetric with crash/exit
    assert sess._task_delivery == "failed"
    assert sess._terminal_kind == "delivery_failed"

    # exactly ONE surfaced escalation that reaches the orchestrator (never silent)
    fails = [e for e in ev._events if e.kind == "delivery_failed"]
    assert len(fails) == 1 and fails[0].hint == "startup_no_output"

    # forensic lifecycle record written at teardown, diagnosable after the fact
    recs = _startup_records(buf)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["reason"] == "startup_no_output"
    assert rec["session_id"] == "s1" and rec["executor"] == "demo"
    assert rec["terminal_kind"] == "delivery_failed"
    assert rec["screen_ever_nonempty"] is False          # the pure blank-screen case
    assert rec["threshold"] == 3.0 and rec["elapsed"] > 3.0

    # PASSIVE: never typed a byte or sent a signal to the child
    assert handle.writes == []
    # process group reaped / concurrency slot freed via the terminal callback
    assert freed == ["s1"]


# ---- 2. noise that never classifies into a prompt: same terminal outcome -------------------------

def test_noise_without_classifiable_prompt_fails_terminally(tmp_path):
    # non-empty content that keeps CHANGING and never forms an input box / modal (no ❯+footer, no
    # numbered menu) -> prompt_kind stays "none" and the screen never becomes non-empty+STABLE.
    noise = _render([b"\x1b[2J\x1b[H" + f"loading step {i} ".encode() + b"." * (i + 2) + b"\r\n"
                     for i in range(14)])
    buf = io.StringIO()
    sess, ev, handle = delivery_run(tmp_path, noise, task="do the work", spec=_FastStartupSpec(),
                                    logger=Logger(level="debug", stream=buf))
    assert sess._task_delivery == "failed"
    assert sess._terminal_kind == "delivery_failed"
    assert _has_event(ev, "delivery_failed", "startup_no_output")
    # noise DID render, but never stabilized into a classifiable prompt -> screen_ever_nonempty True
    rec = _startup_records(buf)[0]
    assert rec["screen_ever_nonempty"] is True
    assert handle.writes == []


# ---- 2b. slow-churn that LATCHES saw_stable then never classifies: must still trip -----------------

def test_slow_churning_stable_frame_still_trips(tmp_path):
    # Slow-churn hole (nelix-b5q): non-classifiable output that changes SLOWER than the pump, so each
    # frame repeats across a pump before changing. The old sticky `saw_stable` latch read that repeat
    # as a permanent "sign of life" and disabled the startup deadline forever -> the session hung
    # pre-delivery. The hard injection deadline must trip regardless. Unlike test #2 (a distinct frame
    # every pump, which never latched), here each rendered frame is HELD for two pumps.
    base = _render([b"\x1b[2J\x1b[H" + f"loading step {i} ".encode() + b"." * (i + 2) + b"\r\n"
                    for i in range(1, 6)])
    frames = [f for f in base for _ in range(2)]     # hold each frame 2 pumps -> latches saw_stable
    buf = io.StringIO()
    sess, ev, handle = delivery_run(tmp_path, frames, task="do the work", spec=_FastStartupSpec(),
                                    logger=Logger(level="debug", stream=buf))
    # terminal fail via the startup deadline, despite a stable non-empty frame having been seen
    assert sess._task_delivery == "failed"
    assert sess._terminal_kind == "delivery_failed"
    assert _has_event(ev, "delivery_failed", "startup_no_output")
    rec = _startup_records(buf)[0]
    assert rec["screen_ever_nonempty"] is True        # it DID render (just never classified)
    assert handle.writes == []                         # PASSIVE: never typed / signaled the child


# ---- 3a. guard: a real input box within budget delivers normally (no false trip) -----------------

def test_real_input_box_delivers_and_does_not_trip(monkeypatch, tmp_path):
    # daemon.session.time.sleep is used by the delivery confirm-poll loop; neutralize for a
    # fast, deterministic drive.
    monkeypatch.setattr("daemon.session.time.sleep", lambda *a, **k: None)
    frames = raw_frames("s-b8a30317-delivery.raw")       # a REAL Claude delivery capture
    sess, ev, handle = delivery_run(tmp_path, frames, task="create a util logging.py")
    assert sess._task_delivery == "delivered"
    assert not _has_event(ev, "delivery_failed")         # the startup deadline never tripped


# ---- 3b. guard: a modal still routes through _emit_blocked (no false trip past the deadline) ------

def test_modal_routes_through_blocked_and_does_not_trip(tmp_path):
    trust = ("Quick safety check: Is this a project you created or one you trust?\r\n"
             "❯ 1. Yes, I trust this folder\r\n  2. No, exit\r\n"
             "Enter to confirm · Esc to cancel\r\n")
    modal = _render([b"\x1b[2J\x1b[H" + trust.encode()])[0]
    # hold the modal well past the (small) startup deadline: the _blocked_fp guard must keep the
    # startup backstop from firing even though the injected clock crosses startup_timeout_seconds.
    sess, ev, handle = delivery_run(tmp_path, [modal], task="do the work",
                                    spec=_FastStartupSpec(), pad_last=8)
    assert sess._task_delivery == "pending"
    assert _has_event(ev, "blocked")                     # routed through _emit_blocked
    assert not _has_event(ev, "delivery_failed")         # the startup deadline did NOT trip on a modal
