"""Golden-capture replay (spec §9 tier 1) — the missing test tier that would have caught F1+F2.

A recorded TIMESTAMPED byte stream is replayed through the REAL renderer + ClaudeDriver.observe +
the pure BeliefEngine, driven by a FakeClock advanced by the capture offsets (no real sleeps). The
asserted DECISION TRAIL is the regression: no spurious waiting_for_user while our submission is still
echoed in the box (F1); the T7 numbered menu surfaces as a modal_choice, not free-text prose (F2); no
intervention_required while the heartbeat is live (F3).

Fixture build command (the raw has no timestamps, so per-chunk offsets are synthesized):

    from daemon.capture import synthesize_capture
    raw = open(".../sessions/s-beb967e9/raw", "rb").read()   # real claude T6/T7 run, now stopped
    synthesize_capture(raw, "tests/golden/claude/_regression/s-beb967e9.capture",
                       chunk_size=4096, dt=0.3)               # uniform 0.3s inter-chunk delta

The capture is replayed at 120x40 (the session's meta.json dims).
"""
import re
from pathlib import Path

from daemon.capture import read_capture
from daemon.renderer.ghostty import GhosttyRenderer
from daemon.drivers.claude import ClaudeDriver
from daemon.observation import ObservationCtx
from daemon.belief import BeliefEngine, Publish, Withdraw
from daemon.config import BeliefConfig
from daemon.clock import FakeClock

FIXTURE = Path(__file__).resolve().parent / "golden" / "claude" / "_regression" / "s-beb967e9.capture"
_MENU_CURSOR = re.compile(r"❯\s*\d+\.")


def _box_text(frame):
    """The text in the active free-text input box (after the last ❯), or "" if that line is a
    numbered-menu cursor (a modal, not the input box). In a real session only the orchestrator types
    here, so non-empty box text IS our submission echo."""
    idx = frame.rfind("❯")
    if idx < 0:
        return ""
    line = frame[idx:].split("\n", 1)[0]
    if _MENU_CURSOR.match(line):
        return ""
    return line[1:].strip()


def replay(capture_path, *, cols=120, rows=40, dt=0.3):
    """Replay a timestamped capture through renderer+driver+engine; return the decision trail.

    Each trail entry is the engine action plus the observation that drove it (the §8 trail IS the
    test oracle). Submit edges are reconstructed from the box echo: when our text appears in the box
    we fire engine.on_submit (mirroring Session calling on_submit at delivery/respond)."""
    renderer = GhosttyRenderer(cols, rows)
    driver = ClaudeDriver()
    clock = FakeClock(0.0)
    engine = BeliefEngine(BeliefConfig(), clock)
    trail = []
    prev_box = ""
    try:
        for offset, chunk in read_capture(capture_path):
            clock.advance(dt)
            renderer.feed(chunk)
            frame = renderer.render()
            box = _box_text(frame)
            if box and box != prev_box:
                engine.on_submit(box)                  # our submission landed in the box (submit edge)
            ctx = ObservationCtx(last_submitted_text=(box or None), child_alive=True, exit_code=None)
            obs = driver.observe(frame, ctx)
            for a in engine.tick(obs, ctx):
                if isinstance(a, (Publish, Withdraw)):
                    trail.append({
                        "offset": offset,
                        "action": "publish" if isinstance(a, Publish) else "withdraw",
                        "kind": getattr(a, "kind", None),
                        "prompt_kind": obs.prompt_kind,
                        "echo": obs.submitted_echo_present,
                        "liveness": engine.state.liveness,
                        "decision_prompt_kind": (a.payload.get("prompt_kind")
                                                 if isinstance(a, Publish) else None),
                    })
            prev_box = box
    finally:
        renderer.close()
    return trail


def replay_notes(capture_path, *, cols=120, rows=40, dt=0.3):
    """Replay the capture through the REAL engine and collect the diagnostic NOTES it drains each tick
    (nelix-jwv): the suppression rationale + post-submit window edges. This is the real-capture oracle
    for the observability records — the same byte stream the F1 trail asserts, now also asserting that
    the post-submit echo windows leave an EXPLICIT suppressed-because record instead of silence."""
    renderer = GhosttyRenderer(cols, rows)
    driver = ClaudeDriver()
    clock = FakeClock(0.0)
    engine = BeliefEngine(BeliefConfig(), clock)
    notes = []
    prev_box = ""
    try:
        for offset, chunk in read_capture(capture_path):
            clock.advance(dt)
            renderer.feed(chunk)
            frame = renderer.render()
            box = _box_text(frame)
            if box and box != prev_box:
                engine.on_submit(box)
            ctx = ObservationCtx(last_submitted_text=(box or None), child_alive=True, exit_code=None)
            obs = driver.observe(frame, ctx)
            engine.tick(obs, ctx)
            for n in engine.drain_notes():
                notes.append({"offset": offset, "event": n.event, **n.fields})
            prev_box = box
    finally:
        renderer.close()
    return notes


def test_post_submit_windows_leave_explicit_suppression_records():
    # nelix-jwv: the F1 post-submit echo windows (our submission lingering in the box) must now be an
    # EXPLICIT belief_suppressed(submitted_echo_present) record, not silence — bracketed by the
    # post_submit_armed edge. A silent stall is therefore diagnosable from the log alone.
    notes = replay_notes(FIXTURE)
    events = {n["event"] for n in notes}
    assert "post_submit_armed" in events, "a submit edge must arm post-submit suppression in the trail"
    echo = [n for n in notes if n["event"] == "belief_suppressed"
            and n.get("reason") == "submitted_echo_present"]
    assert echo, "the post-submit echo windows must surface as explicit suppressed-because records"


def _publishes(trail, kind="waiting_for_user"):
    return [t for t in trail if t["action"] == "publish" and t["kind"] == kind]


def test_fixture_exists():
    assert FIXTURE.exists(), f"missing golden capture fixture {FIXTURE} (see build command in header)"


def test_no_spurious_waiting_for_user_in_post_submit_windows():
    # F1: the daemon must NOT publish waiting_for_user while our just-submitted text is still echoed
    # in the input box (the post-submit/TTFT gap). Before the fix, 2 of 5 idle edges were spurious.
    trail = replay(FIXTURE)
    spurious = [t for t in _publishes(trail) if t["echo"]]
    assert spurious == [], f"spurious waiting_for_user while our echo was in the box: {spurious}"


def test_t7_menu_surfaces_as_modal_choice():
    # F2: the agent's numbered "ask the user" menu (T7) must surface as a modal_choice (with options
    # routed to select_option), NOT a free-text prompt the orchestrator answers with prose.
    trail = replay(FIXTURE)
    modal = [t for t in trail if t["action"] == "publish"
             and t["decision_prompt_kind"] == "modal_choice"]
    assert modal, "the T7 numbered menu must surface as a modal_choice decision"


def test_no_intervention_required_while_heartbeat_live():
    # F3: a healthy session whose spinner keeps animating (heartbeat live) is never escalated as
    # stuck/hung. No intervention_required at all on this capture, and certainly none while live.
    trail = replay(FIXTURE)
    inter = [t for t in trail if t["kind"] == "intervention_required"]
    assert inter == [], f"unexpected intervention_required on a live, healthy session: {inter}"
    inter_live = [t for t in inter if t["liveness"] == "live"]
    assert inter_live == []
