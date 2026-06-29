from pathlib import Path

from daemon.pty_session import PtySession
from daemon.transcript_builder import TranscriptBuilder
from daemon.drivers.claude import ClaudeDriver

RAW = Path("tests/golden/claude/_regression/3p1_alt_screen.raw")


class _Dialog:
    def __init__(self): self.lines = []
    def append_raw(self, chunk): pass
    def add_line(self, text): self.lines.append(text)


def _replay():
    d = _Dialog()
    tb = TranscriptBuilder(d, ClaudeDriver(), 40)
    s = PtySession(None, 0, 0, cols=120, rows=40, dialog=d, transcript=tb)
    raw = RAW.read_bytes()
    for i in range(0, len(raw), 4096):          # feed in chunks like the live pump
        s._feed(raw[i:i + 4096])
    s.finalize()
    s.close()
    return d.lines


def test_replay_transcript_is_clean_and_ordered():
    lines = _replay()
    assert lines, "transcript should not be empty for an alt-screen session"
    # 1. Low duplication (occurrence tracking; commit-on-eviction; repeated tool steps expected).
    dup_ratio = len(lines) / max(1, len(set(lines)))
    assert dup_ratio < 6.0, f"dup_ratio {dup_ratio:.2f} too high"
    # 2. No volatile chrome leaked into the transcript.
    blob = "\n".join(lines)
    assert "shift+tab to cycle" not in blob
    assert "esc to interrupt" not in blob
    assert " · ↓ " not in blob          # spinner telemetry token counter (e.g. "↓ 4.0k tokens")
    # 3. Known conversation content is present (this raw is the meshynet T3 session).
    assert any("establish_phase" in ln for ln in lines)
    assert any("Committed" in ln for ln in lines)
