"""Real-capture regression: a running BACKGROUND SUBAGENT must not be read as `waiting_for_user`.

Replays an actual captured session (`s-039a61b4`, Claude Code v2.1.x) in which the daemon flapped
`publish:waiting_for_user` / `withdraw:prompt_changed` ~35x over ~8 minutes while a `golang-pro`
background subagent ran. While that subagent runs, Claude shows BOTH a live input box (`❯` + footer)
AND a status line `✻ Waiting for N background agent(s) to finish`, with the subagent's live
`… · ↓ NN.Nk tokens` ticker rendered BELOW the `❯` row. The driver classified those frames as
`free_text` (→ the engine published a respondable `waiting_for_user`), and the ticker churned the
prompt fingerprint every tick (→ the flap). The session is busy, not awaiting the user.

Drives the REAL renderer over the REAL bytes (fabricated frames missed exactly this class of bug).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.renderer.ghostty import GhosttyRenderer   # noqa: E402
from daemon.drivers.claude import ClaudeDriver         # noqa: E402
from daemon.observation import ObservationCtx          # noqa: E402

_CAPTURE = Path(__file__).parent / "golden" / "claude" / "_regression" / "s-039a61b4-bg-subagent.raw"
_CTX = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)


def _distinct_frames(raw, cols=120, rows=40):
    drv = ClaudeDriver()
    r = GhosttyRenderer(cols, rows)
    seen = None
    for i in range(0, len(raw), 256):
        r.feed(raw[i:i + 256])
        frame = r.render()
        if frame == seen:
            continue
        seen = frame
        yield frame, drv.observe(frame, _CTX)


def test_background_subagent_frames_are_never_waiting_for_user():
    raw = _CAPTURE.read_bytes()
    bg_frames = wrong = 0
    for frame, obs in _distinct_frames(raw):
        if "background agent to finish" not in frame:
            continue
        bg_frames += 1
        if obs.prompt_kind == "free_text":             # the bug: busy read as awaiting-user
            wrong += 1
    assert bg_frames > 100, f"fixture should exercise the bg-subagent window (saw {bg_frames})"
    # RED before the fix: ~341 of these frames classify free_text. GREEN after: zero.
    assert wrong == 0, f"{wrong}/{bg_frames} background-subagent frames misread as free_text"


def test_background_subagent_window_ends_in_a_wakeable_state():
    # Faithfulness guard: the capture must also contain non-bg frames AFTER the subagent finishes,
    # so the fix is shown to SUPPRESS the wake only while busy — not to swallow the session forever.
    raw = _CAPTURE.read_bytes()
    frames = [f for f, _ in _distinct_frames(raw)]
    assert any("background agent to finish" in f for f in frames)
    assert any("background agent to finish" not in f for f in frames[-50:])
