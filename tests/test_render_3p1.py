"""nelix-3p1 faithful-render regression.

pyte rendered this capture (session s-e54b456e, 120x40, Claude Code alt-screen) with
stale/overlapping chars on 24/40 rows — garbled Cyrillic commit summaries, doubled
ASCII fragments in footer lines.  libghostty-vt renders it clean; that was validated
against xterm.js in spike nelix-ks5.

This test asserts three invariants from the actual ghostty-rendered frame:

1. Viewport shape: exactly 40 '\\n'-separated rows (rectangular terminal).
2. Cyrillic content line intact: row 8 contains the summary line "⏺ Готово…" exactly as
   libghostty-vt renders it.  pyte garbled this row (stale bytes left overwrite artefacts).
3. Commit message line intact: row 12 contains the full ASCII commit subject exactly.
   pyte also garbled this row.  Two independently-chosen clean lines make it hard to
   write a renderer that passes both by accident.

All three assertions are falsifiable: a garbling regression will break at least one.
The test is deterministic and self-contained (reads only the committed fixture; no live
process, no network).
"""
from pathlib import Path

import pytest

from daemon.pty_session import render_raw

FIXTURE = Path(__file__).resolve().parent / "golden" / "claude" / "_regression" / "3p1_alt_screen.raw"


@pytest.fixture(scope="module")
def rendered() -> list[str]:
    raw = FIXTURE.read_bytes()
    out = render_raw(raw, cols=120, rows=40)
    return out.split("\n")


def test_viewport_is_rectangular(rendered):
    """Ghostty emits exactly `rows` lines — the viewport is a clean 40-row rectangle."""
    assert len(rendered) == 40, (
        f"expected 40 rows, got {len(rendered)} — viewport shape broken"
    )


def test_cyrillic_summary_line_intact(rendered):
    """Row 8 must contain the Cyrillic completion marker exactly as ghostty rendered it.

    pyte left stale bytes on this row so the Cyrillic was garbled/overwritten.
    The exact string was verified from the libghostty-vt render in spike nelix-ks5.
    """
    expected = "⏺ Готово. Закоммитил T3 на ветке phase-no1.3."
    assert rendered[8] == expected, (
        f"Cyrillic summary garbled — got: {rendered[8]!r}"
    )


def test_commit_subject_line_intact(rendered):
    """Row 12 must contain the ASCII commit subject line intact.

    pyte garbled this row too; ghostty renders all 40 rows cleanly.  Two independently-
    chosen distinctive lines (Cyrillic + ASCII) make it hard for a broken renderer to
    pass by coincidence.
    """
    expected = "  feat(conn,no1.3): initiator establish_phase emission in Manager.Dial + fail-path join test"
    assert rendered[12] == expected, (
        f"Commit subject garbled — got: {rendered[12]!r}"
    )
