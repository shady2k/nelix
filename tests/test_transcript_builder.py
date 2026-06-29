from dataclasses import dataclass
from daemon.transcript_builder import TranscriptBuilder


@dataclass
class _Frame:
    rows: list


class _Dialog:
    """Fake dialog that exposes add_agent_line (flat-log API) and tracks records."""

    def __init__(self):
        self.records = []     # list of (kind, text) — simplified flat log
        self._current_speaker = None

    def add_agent_line(self, text):
        # Mirror the real Dialog: emit ‹agent› marker on transition, then the line.
        if self._current_speaker != "agent":
            self.records.append(("marker", "‹agent›"))
            self._current_speaker = "agent"
        self.records.append(("line", text))

    @property
    def lines(self):
        """Content lines only (kind == "line"), for backward-compatible assertions."""
        return [t for (k, t) in self.records if k == "line"]

    @property
    def agent_markers(self):
        return [t for (k, t) in self.records if k == "marker" and t == "‹agent›"]


class _Driver:
    # volatile = a row that starts with "SPIN " (stands in for the spinner status line)
    def is_transcript_volatile(self, row): return row.startswith("SPIN ")


def _b(rows_height=5, **kw):
    return TranscriptBuilder(_Dialog(), _Driver(), rows_height, **kw)


def _frame(rows, h=5):
    return _Frame(rows=(list(rows) + [""] * h)[:h])


def test_line_committed_once_on_eviction():
    d = _Dialog(); b = TranscriptBuilder(d, _Driver(), 3, stable=2, grace=2)
    # "A" present 3 frames (stable), then scrolls away for >= grace frames
    for _ in range(3):
        b.observe(_frame(["A", "B"], 3))
    for _ in range(3):
        b.observe(_frame(["C", "D"], 3))     # A and B gone
    assert d.lines.count("A") == 1 and d.lines.count("B") == 1


def test_spinner_churn_not_committed():
    d = _Dialog(); b = TranscriptBuilder(d, _Driver(), 3, stable=2, grace=2)
    for i in range(10):
        b.observe(_frame([f"SPIN {i}"], 3))   # only volatile rows
    b.finalize()
    assert d.lines == []


def test_reflow_jitter_not_double_committed():
    d = _Dialog(); b = TranscriptBuilder(d, _Driver(), 3, stable=2, grace=3)
    b.observe(_frame(["A"], 3)); b.observe(_frame(["A"], 3))   # stable
    b.observe(_frame([""], 3))                                  # A blinks out 1 frame (< grace)
    b.observe(_frame(["A"], 3)); b.observe(_frame(["A"], 3))   # returns, same object
    for _ in range(4):
        b.observe(_frame(["Z"], 3))                             # now A truly gone
    assert d.lines.count("A") == 1


def test_finalize_commits_tail_even_if_seen_once():
    d = _Dialog(); b = TranscriptBuilder(d, _Driver(), 3, stable=2, grace=2)
    b.observe(_frame(["only-once"], 3))        # seen == 1 < stable
    b.finalize()
    assert d.lines == ["only-once"]
    b.finalize()                               # idempotent: nothing new
    assert d.lines == ["only-once"]


def test_legitimate_repeat_committed_twice():
    d = _Dialog(); b = TranscriptBuilder(d, _Driver(), 3, stable=2, grace=2)
    for _ in range(2): b.observe(_frame(["go"], 3))      # occurrence 1 stable
    for _ in range(3): b.observe(_frame(["x"], 3))        # evicts -> commit "go"
    for _ in range(2): b.observe(_frame(["go"], 3))      # occurrence 2 (new) stable
    for _ in range(3): b.observe(_frame(["y"], 3))        # evicts -> commit "go" again
    assert d.lines.count("go") == 2


def test_two_identical_rows_in_one_frame_both_committed():
    d = _Dialog(); b = TranscriptBuilder(d, _Driver(), 4, stable=1, grace=2, match_window=1)
    b.observe(_frame(["dup", "dup"], 4))       # two objects at y=0 and y=1
    for _ in range(3): b.observe(_frame(["z"], 4))
    assert d.lines.count("dup") == 2


def test_agent_transition_marker_appears_once_per_span():
    """‹agent› should appear exactly once per agent span, not per committed line."""
    d = _Dialog(); b = TranscriptBuilder(d, _Driver(), 3, stable=2, grace=2)
    # Emit several agent lines in a single uninterrupted span
    for _ in range(3):
        b.observe(_frame(["step 1", "step 2"], 3))
    for _ in range(3):
        b.observe(_frame(["step 3"], 3))    # scrolls out step 1 & 2
    b.finalize()
    assert len(d.agent_markers) == 1, (
        f"expected exactly 1 ‹agent› marker for an uninterrupted span, got {len(d.agent_markers)}: "
        f"{d.records}"
    )
    assert len(d.lines) >= 1


def test_agent_marker_repeats_on_new_span():
    """If a new agent span starts after the dialog's speaker is reset, a new ‹agent› appears."""
    # Simulate: agent lines committed, then dialog speaker reset to user between two builder runs.
    d = _Dialog()
    b1 = TranscriptBuilder(d, _Driver(), 3, stable=1, grace=2)
    b1.observe(_frame(["span1"], 3)); b1.observe(_frame(["span1"], 3))
    b1.finalize()
    assert len(d.agent_markers) == 1     # first span

    # Simulate a user input resetting the speaker
    d._current_speaker = "user"
    b2 = TranscriptBuilder(d, _Driver(), 3, stable=1, grace=2)
    b2.observe(_frame(["span2"], 3)); b2.observe(_frame(["span2"], 3))
    b2.finalize()
    assert len(d.agent_markers) == 2     # second span gets its own marker
