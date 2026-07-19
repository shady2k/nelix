"""I5 real-capture volatile-row test (nelix-5gc Task 4, review fix).

Reads the REAL harvested frame tests/golden/claude/working/ctrl-b-panel.txt and verifies
is_transcript_volatile() on:
  (a) a transient chrome row — the "(ctrl+b to run in background)" background-hint line, which
      is in-progress tool-status chrome that flashes and is replaced;
  (b) a settled content row — the "RED confirmed" agent output line (a ⏺ turn with content,
      not a bare marker, not ending in ellipsis, not matching any volatile pattern).

This test is complementary to the ctrl-b-panel observe() fixture (ctrl-b-panel.yaml), which
checks observe() fields; this file checks the is_transcript_volatile() contract directly.

RED proof (recorded for commit body — DO NOT re-run, this is historical):
  Mutation: changed `_BACKGROUND_HINT = "(ctrl+b to run in background)"` to
            `_BACKGROUND_HINT = "XBROKEN"`  in daemon/drivers/claude.py
  Command:  .venv/bin/python -m pytest tests/test_i5_chrome_volatile.py::test_background_hint_row_is_volatile -v
  Result:   FAILED — AssertionError: Expected volatile for background-hint row
  Revert:   restored original value immediately.
"""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.drivers.claude import ClaudeDriver  # noqa: E402

_FRAME_PATH = (
    Path(__file__).resolve().parent / "golden" / "claude" / "working" / "ctrl-b-panel.txt"
)
_DRIVER = ClaudeDriver()


def _rows():
    return _FRAME_PATH.read_text().splitlines()


def _find_row(rows, substr):
    for r in rows:
        if substr in r:
            return r
    raise AssertionError(
        f"No row containing {substr!r} found in ctrl-b-panel.txt — frame drift?"
    )


def test_background_hint_row_is_volatile():
    """The '(ctrl+b to run in background)' line is transient chrome — must be volatile."""
    rows = _rows()
    row = _find_row(rows, "(ctrl+b to run in background)")
    assert _DRIVER.is_transcript_volatile(row) is True, (
        f"Expected volatile for background-hint row: {row!r}"
    )


def test_settled_content_row_is_not_volatile():
    """A settled ⏺ output line (no ellipsis, no spinner, no chrome markers) must NOT be volatile."""
    rows = _rows()
    # "RED confirmed" is the start of a ⏺ output line with no volatile chrome characteristics.
    row = _find_row(rows, "RED confirmed")
    assert _DRIVER.is_transcript_volatile(row) is False, (
        f"Expected NOT volatile for settled content row: {row!r}"
    )
