import re
from pathlib import Path

from daemon.pty_session import PtySession
from daemon.transcript_builder import TranscriptBuilder
from daemon.drivers.claude import ClaudeDriver
from daemon.dialog import Dialog

RAW = Path("tests/golden/claude/_regression/3p1_alt_screen.raw")


class _Dialog:
    """Minimal dialog shim that exposes the flat-log API for replay tests."""

    def __init__(self):
        self.records = []   # list of (kind, text)
        self._current_speaker = None

    def append_raw(self, chunk):
        pass

    def add_agent_line(self, text):
        if self._current_speaker != "agent":
            self.records.append(("marker", "‹agent›"))
            self._current_speaker = "agent"
        self.records.append(("line", text))

    def append_user_input(self, text):
        self.records.append(("marker", "» " + text))
        self._current_speaker = "user"
        return 0

    @property
    def lines(self):
        return [t for (k, t) in self.records if k == "line"]

    @property
    def markers(self):
        return [t for (k, t) in self.records if k == "marker"]

    def page(self, offset=0, limit=None):
        text = "\n".join(t for (_, t) in self.records)
        total = len(text)
        chunk = text[offset:] if offset < total else ""
        if limit and len(chunk) > limit:
            chunk = chunk[:limit]
        return {"text": chunk, "next_offset": total, "total_len": total}


def _replay(task="meshynet establish_phase task"):
    d = _Dialog()
    tb = TranscriptBuilder(d, ClaudeDriver(), 40)
    s = PtySession(None, 0, 0, cols=120, rows=40, dialog=d, transcript=tb)
    raw = RAW.read_bytes()
    for i in range(0, len(raw), 4096):          # feed in chunks like the live pump
        s._feed(raw[i:i + 4096])
    s.finalize()
    # Simulate the user task marker (as session._deliver_task does)
    d.append_user_input(task)
    s.close()
    return d


def test_replay_transcript_is_clean_and_ordered():
    d = _replay()
    lines = d.lines
    assert lines, "transcript should not be empty for an alt-screen session"
    # 1. Low duplication (occurrence tracking; commit-on-eviction; repeated tool steps expected).
    #    Occurrence tracking legitimately preserves cross-turn repeats (~1.7 expected after fix);
    #    4.2 was the transient-flood bug (ellipsis/bare-⏺/ctrl+b chrome) now masked.
    dup_ratio = len(lines) / max(1, len(set(lines)))
    assert dup_ratio < 2.0, f"dup_ratio {dup_ratio:.2f} too high"
    # 2. No volatile chrome leaked into the transcript.
    blob = "\n".join(lines)
    assert "shift+tab to cycle" not in blob
    assert "esc to interrupt" not in blob
    assert " · ↓ " not in blob          # spinner telemetry token counter (e.g. "↓ 4.0k tokens")
    # 2b. Strong invariant: no committed line ends in an ellipsis (transient status never committed).
    assert not any(re.search(r"(?:…|\.\.\.)\s*$", ln) for ln in lines), \
        "ellipsis-tailed (in-progress) line committed to transcript"
    # 3. Known conversation content is present (this raw is the meshynet T3 session).
    assert any("establish_phase" in ln for ln in lines)
    assert any("Committed" in ln for ln in lines)


def test_replay_transcript_starts_with_markers():
    """Rendered transcript starts with ‹agent› then agent output (markers on transitions)."""
    d = _replay()
    markers = d.markers
    assert any(m == "‹agent›" for m in markers), "expected at least one ‹agent› marker"
    # The first record in the full flat log should be the initial ‹agent› marker (user_input
    # is appended after finalize in this replay harness, so agent comes first)
    assert d.records[0] == ("marker", "‹agent›"), (
        f"first record should be ‹agent› marker, got {d.records[0]}")


def test_replay_full_pagination_reproduces_text():
    """Paginating the whole log via next_offset reproduces the full flat text exactly."""
    d = _replay()
    full_text = "\n".join(t for (_, t) in d.records)
    total_len = len(full_text)

    # Use a real Dialog to get the flat-log pagination
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        import paths
        dlg = Dialog(td + "/s", tail_lines=1000, spool_max_bytes=10_000_000)
        # Replay all records into the real Dialog
        for (kind, text) in d.records:
            if kind == "marker" and text.startswith("» "):
                dlg.append_user_input(text[2:])
            elif kind == "marker" and text == "‹agent›":
                pass   # the Dialog emits its own marker via add_agent_line transitions
            else:
                dlg.add_agent_line(text)
        # Paginate through the whole log
        parts = []
        off = 0
        while True:
            p = dlg.page(off, limit=4096)
            if p["text"]:
                parts.append(p["text"])
            if p["next_offset"] >= p["total_len"]:
                break
            off = p["next_offset"]
        reconstructed = "".join(parts)
        full = dlg.page()["text"]
        assert reconstructed == full, "paginating via next_offset must reproduce the full text"
        dlg.close()
