"""Real-capture delivery confirmation test.

Delivery confirmation (`_deliver_task` -> `observe().submitted_echo_present`) must recognise a
landed bracketed-paste in REAL Claude Code output, not just a hand-fabricated frame. This replays
an actual captured session (`s-b8a30317`, Claude Code v2.1.195) whose delivery the daemon FAILED to
confirm: Claude renders the placeholder as `❯<NBSP>[Pasted text #1]` (NBSP = U+00A0 between the
marker and the placeholder), which the `_PASTED_TEXT` regex must match. Drives the real renderer.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._replay import replay_frames               # noqa: E402
from daemon.drivers.claude import ClaudeDriver         # noqa: E402

_CAPTURE = Path(__file__).parent / "golden" / "claude" / "_regression" / "s-b8a30317-delivery.raw"


def _confirms_during_replay(raw, task, cols=120, rows=40):
    """Mirror `_deliver_task`'s confirm loop: render the capture incrementally and report whether
    `submitted_echo_present` ever becomes True (as the daemon would, polling each pump)."""
    drv = ClaudeDriver()
    for _, frame in replay_frames(raw, cols=cols, rows=rows, chunk=256):
        if drv._echo_present(frame, task):
            return True
    return False


def test_real_bracketed_paste_is_confirmed():
    # The exact session whose delivery failed in production. The placeholder renders with an NBSP;
    # confirmation MUST recognise it. (RED before the _PASTED_TEXT NBSP fix, GREEN after.)
    raw = _CAPTURE.read_bytes()
    assert _confirms_during_replay(raw, "a task that gets bracketed-pasted and collapsed") is True
