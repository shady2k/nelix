"""Real-capture regression (nelix-4q8): a partial-redraw remnant must not be double-committed.

Replays the recorded session ``s-9610d25c`` — an AskUserQuestion choice modal opening over a
freshly streamed agent block — through the REAL renderer (``GhosttyRenderer``) + REAL
``TranscriptBuilder`` via ``PtySession._feed``, the exact capture path the live daemon runs (fed at
the recorded pump-read boundaries via ``read_capture``; NO fabricated frames).

The bug: Claude paints the choice modal over the streamed block from the TOP DOWN, so one captured
frame is TORN — the modal's fresh rows sit ABOVE stale remnants of the block's tail the repaint has
not overwritten yet (in that frame the lines "…renewal blocks again on that name." and "…do you want
me to act now?" are each present at BOTH their real row AND, 13 rows lower under the modal border, as
an un-painted-over remnant). The daemon commits the visible tail (``finalize()``) the instant it
detects the blocked modal — landing on that torn frame — so the coalescer APPENDED the block's
suffix twice (live ``transcript.jsonl`` 67-86, then again 87-102).

The fix (``TranscriptBuilder._content_rows``): a duplicate line separated from its first occurrence
by CHROME (the modal border / ❯ prompt) is a repaint remnant and is dropped, keeping the topmost real
occurrence. RED (pre-fix): the "…act now?" suffix commits twice. GREEN: every rendered content line
appears exactly once.

The companion ``content_fp`` invariant (the frozen ``4d377be6206b1bf3``): once the modal settles the
executor-output fingerprint is STABLE and its final value never appeared earlier in the stream — it
is frozen at the end, not a transient value that recurred.
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.pty_session import PtySession                 # noqa: E402
from daemon.transcript_builder import TranscriptBuilder   # noqa: E402
from daemon.drivers.claude import ClaudeDriver            # noqa: E402
from daemon.capture import read_capture                   # noqa: E402
from daemon.observation import ObservationCtx             # noqa: E402
from tests._replay import replay_frames                   # noqa: E402

_CAP = Path(__file__).parent / "golden" / "claude" / "_regression" / \
    "s-9610d25c-askuserquestion-collision.capture"

# The agent block that was duplicated. Both remnant lines (the tail and the line 2 rows above it) and
# a line from higher up in the block are checked, so the assertion covers the whole committed block —
# not just the "act now?" suffix.
_SUFFIX = "Since the cert is already expired, do you want me to act now?"
_BLOCK_LINES = (
    "Explicit reload instead of notify",                    # near the top of the committed block
    "any SAN's DNS later drifts, renewal blocks again",     # the OTHER duplicated remnant line
    _SUFFIX,                                                 # the block's tail (67-86 / 87-102)
)


class _RecordingDialog:
    """Flat-log shim mirroring the real Dialog's add_agent_line surface (see test_transcript_replay)."""

    def __init__(self):
        self.lines = []

    def append_raw(self, chunk):
        pass

    def add_agent_line(self, text):
        self.lines.append(text)


def _modal_on_screen(frame):
    """The AskUserQuestion choice modal is up: its question and first numbered option are rendered.
    This is the point the live Session commits the visible tail (``_apply_publish`` -> finalize) on
    the blocked decision — the first, still-torn frame of the modal's top-down repaint."""
    return "How do you want to proceed?" in frame and "1. Apply patch to repo" in frame


def _replay_transcript():
    """Feed the capture at its recorded read boundaries through the real renderer + TranscriptBuilder,
    committing the visible tail when the blocked modal is first detected (as the daemon does), then a
    final commit at session stop. Returns the flat list of committed content lines."""
    dialog = _RecordingDialog()
    tb = TranscriptBuilder(dialog, ClaudeDriver(), 40)
    pty = PtySession(None, 0, 0, cols=120, rows=40, dialog=dialog, transcript=tb)
    committed_on_modal = False
    for _, chunk in read_capture(_CAP):
        pty._feed(chunk)
        if not committed_on_modal and _modal_on_screen(pty.render()):
            pty.finalize()                 # Session commits the visible tail at the blocked decision
            committed_on_modal = True
    pty.finalize()                         # session stop
    pty.close()
    return dialog.lines, committed_on_modal


def test_partial_redraw_suffix_committed_once():
    lines, committed_on_modal = _replay_transcript()

    # Non-vacuity: the modal WAS detected (so the tail commit really landed on the torn frame) and the
    # block really made it into the transcript — otherwise a silent behaviour change could pass this
    # test without ever exercising the redraw.
    assert committed_on_modal, "AskUserQuestion modal never detected — fixture or renderer changed"
    for probe in _BLOCK_LINES:
        assert any(probe in ln for ln in lines), f"block line missing from transcript: {probe!r}"

    # The bug: the partial-redraw remnants of the block were APPENDED a second time. Assert the WHOLE
    # committed block (top line + both remnant lines), not just the "act now?" suffix, is single.
    for probe in _BLOCK_LINES:
        n = sum(probe in ln for ln in lines)
        assert n == 1, (f"block line committed {n}x (partial-redraw remnant double-committed): "
                        f"{probe!r}; transcript=\n" + "\n".join(lines))

    # Stronger: this is a single-turn capture, so EVERY rendered content line appears exactly once.
    dups = {ln: n for ln, n in Counter(lines).items() if n > 1}
    assert not dups, f"duplicate committed lines (partial-redraw remnants appended): {dups}"


def test_content_fp_frozen_without_transient_dup():
    """content_fp settles to a stable final value that never recurred mid-stream (the bead's frozen
    4d377be6206b1bf3): once the modal is up the executor-output fingerprint stops changing, and that
    final value is not a transient that appeared earlier and came back."""
    ctx = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)
    drv = ClaudeDriver()
    fps = [drv.observe(frame, ctx).content_fp
           for _, frame in replay_frames(_CAP.with_suffix(".raw").read_bytes())]

    assert len(fps) > 100, f"expected a long frame stream, got {len(fps)}"
    final = fps[-1]
    # The exact frozen fingerprint recorded in the bead for this settled modal.
    assert final == "4d377be6206b1bf3", f"content_fp froze at {final}, expected 4d377be6206b1bf3"
    # Frozen: the tail of the stream holds the final value stably (settled modal, no more churn).
    assert fps[-1] == fps[-2] == final, "content_fp still oscillating at the end (not frozen)"
    # No transient dup: the frozen value never appeared before the terminal settled run.
    first = fps.index(final)
    assert all(fp == final for fp in fps[first:]), (
        "content_fp took its final value, changed away, then returned (transient dup before freeze)")
