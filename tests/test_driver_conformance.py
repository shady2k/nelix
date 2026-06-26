"""Driver-conformance harness (Spec 2): assert the claude driver's classify() against golden frames
captured from real CLI sessions. When Claude Code drifts (e.g. it drops a marker the driver keys on),
this goes RED in dev — instead of the daemon misclassifying a live agent (nelix-48o).

Golden frames live in tests/golden/claude/<expected-classify>/*.txt — the directory name IS the
expected classify() result. Refresh them with bin/nelix-capture; see tests/golden/README.md.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.drivers.claude import ClaudeDriver   # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden" / "claude"
CATEGORIES = ("working", "idle_prompt", "permission_prompt")


class _SettledCtx:
    # The decision-point condition: "if this screen is settled (stable past the settle window) and
    # the child is alive, would we misclassify it?" Raw has no inter-byte timing, so stability is
    # simulated here, not replayed.
    stable_for = 9.9
    bytes_idle_for = 9.9
    child_alive = True
    exit_code = None


def _cases():
    cases = []
    for cat in CATEGORIES:
        files = sorted((GOLDEN / cat).glob("*.txt"))
        assert files, f"no golden frames in {GOLDEN / cat} — a category must not silently pass empty"
        cases.extend((cat, f) for f in files)
    return cases


_CASES = _cases()


@pytest.mark.parametrize("expected,path", _CASES,
                         ids=[str(p.relative_to(GOLDEN)) for _, p in _CASES])
def test_claude_classify_matches_golden(expected, path):
    frame = path.read_text()
    got = ClaudeDriver().classify(frame, _SettledCtx())
    if got != expected:
        head = "\n".join(ln for ln in frame.splitlines() if ln.strip())[:400]
        pytest.fail(f"{path.relative_to(GOLDEN.parent)}: expected {expected!r}, got {got!r}\n"
                    f"--- first non-blank lines ---\n{head}")
