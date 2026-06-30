"""Tier-2 sequence oracles — behaviour across the REAL frame STREAM.

Each oracle replays a committed regression raw through the shared helper, the real
ClaudeDriver, and (for belief tests) the real BeliefEngine, asserting an invariant that
spans the full sequence of observations rather than a single frame.

I2b — bg-subagent session never publishes waiting_for_user            (regression: cd3352d)

Real captures (tests/golden/claude/_regression/):
  s-039a61b4-bg-subagent.raw  — bg subagent running (I2b)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._replay import replay_frames                        # noqa: E402
from daemon.drivers.claude import ClaudeDriver                 # noqa: E402
from daemon.observation import ObservationCtx                  # noqa: E402
from daemon.belief import BeliefEngine, Publish                # noqa: E402
from daemon.clock import FakeClock                             # noqa: E402
from daemon.config import BeliefConfig                         # noqa: E402

_GOLDEN   = Path(__file__).parent / "golden" / "claude" / "_regression"
_BG       = (_GOLDEN / "s-039a61b4-bg-subagent.raw").read_bytes()

_CTX_PLAIN = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)


# ─────────────────────────────────────────────────────────────────────────────
# I2b — bg-subagent session never publishes waiting_for_user
# ─────────────────────────────────────────────────────────────────────────────

def _count_waiting_publishes(raw, ctx, *, cfg=None):
    """Replay `raw` through renderer+driver+BeliefEngine; return count of
    waiting_for_user Publish actions emitted."""
    cfg = cfg or BeliefConfig()
    drv, clk = ClaudeDriver(), FakeClock(0.0)
    eng = BeliefEngine(cfg, clk)
    n = 0
    for _, frame in replay_frames(raw):
        obs = drv.observe(frame, ctx)
        clk.advance(1.0)
        for a in eng.tick(obs, ctx):
            if isinstance(a, Publish) and a.kind == "waiting_for_user":
                n += 1
    return n


def test_bg_subagent_never_publishes_waiting_for_user():
    """While a background subagent runs, the screen is busy — the engine must NOT tell the
    orchestrator the user is needed.  Regression: cd3352d (bg frames classified free_text →
    engine published waiting_for_user while the turn was blocked on a golang-pro subagent).

    RED command:
        git show cd3352d~1:daemon/drivers/claude.py > daemon/drivers/claude.py
        .venv/bin/python -m pytest tests/test_sequence_oracles.py::test_bg_subagent_never_publishes_waiting_for_user -v
    RED result: 61 waiting_for_user publishes → assert 61 == 0 → FAIL.
    Restore: git checkout daemon/drivers/claude.py.

    GREEN: 0 waiting_for_user publishes across the full 570 kB replay.
    """
    assert _count_waiting_publishes(_BG, _CTX_PLAIN) == 0
