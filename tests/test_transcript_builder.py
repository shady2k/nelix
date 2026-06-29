from dataclasses import dataclass
from daemon.transcript_builder import TranscriptBuilder


@dataclass
class _Frame:
    rows: list


class _Dialog:
    def __init__(self): self.lines = []
    def add_line(self, text): self.lines.append(text)


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
    b.observe(_frame([""], 3))                                 # A blinks out 1 frame (< grace)
    b.observe(_frame(["A"], 3)); b.observe(_frame(["A"], 3))   # returns, same object
    for _ in range(4):
        b.observe(_frame(["Z"], 3))                            # now A truly gone
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
