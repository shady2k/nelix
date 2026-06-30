"""Tier-2 sequence oracles — behaviour across the REAL frame STREAM.

Each oracle replays a committed regression raw through the shared helper, the real
ClaudeDriver, and (for belief tests) the real BeliefEngine, asserting an invariant that
spans the full sequence of observations rather than a single frame.

I2b — bg-subagent session never publishes waiting_for_user            (regression: cd3352d)
I8  — post-submit echo window suppressed (no false-idle publish)       (regression: 6de482c)

Real captures (tests/golden/claude/_regression/):
  s-039a61b4-bg-subagent.raw  — bg subagent running (I2b)
  s-b8a30317-delivery.raw     — paste delivery, echo visible post-submit (I8)
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
_DELIVERY = (_GOLDEN / "s-b8a30317-delivery.raw").read_bytes()

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


# ─────────────────────────────────────────────────────────────────────────────
# I8 — post-submit echo window suppressed (no false-idle publish)
# ─────────────────────────────────────────────────────────────────────────────

def test_post_submit_echo_suppresses_waiting_for_user():
    """After the user submits text the echoed placeholder lingers in the active input box
    during TTFT; the engine must NOT publish waiting_for_user while that echo is visible.
    Regression: 6de482c (no submitted_echo_present suppression → false-idle publish).

    on_submit() is NOT called here: when the delivery capture is replayed from the start,
    initial screen transitions change content_fp before the echo frames, clearing
    _post_submit_active via check (b) in _update_post_submit — making on_submit()-based
    suppression vacuous over a cold-start replay.  The assertion is therefore scoped to the
    echo window directly: for any frame where submitted_echo_present=True, the engine must
    emit no waiting_for_user publish.  This is exactly the invariant the
    `if obs.submitted_echo_present:` block in _on_prompt enforces.

    idle_confirm_window=0.0 makes the test non-vacuous: the one echo frame with
    kind=free_text (offset 2048, t=9.0) settles immediately; without the echo check it
    proceeds to _publish_decision (post_submit_active=False, anti-flap cooldown boundary
    9.0 < 9.0 is False, settle=True).

    RED mutation (local, NOT committed):
        In daemon/belief.py, in BeliefEngine._on_prompt(), comment out the six-line block:
            if obs.submitted_echo_present:
                if self._echo_since is None:
                    self._echo_since = now
                if (now - self._echo_since) < self._cfg.echo_stuck_after:
                    self._suppressed("submitted_echo_present")
                    return
                self._escalate_stuck_input(obs, now, actions)
                return
        Then run:
            .venv/bin/python -m pytest tests/test_sequence_oracles.py::test_post_submit_echo_suppresses_waiting_for_user -v
    RED result: offset-2048 frame (kind=free_text, echo=True) settles, no suppression →
    publish fires during echo window → wakes_during_echo=1 → assert 1 == 0 → FAIL.
    Restore: git checkout daemon/belief.py.

    GREEN: 0 waiting_for_user publishes during echo-visible frames.
    """
    ctx = ObservationCtx(last_submitted_text="a task that was pasted", child_alive=True, exit_code=None)
    # idle_confirm_window=0.0: echo frame (kind=free_text, offset 2048) settles immediately.
    cfg = BeliefConfig(idle_confirm_window=0.0)
    drv, clk = ClaudeDriver(), FakeClock(0.0)
    eng = BeliefEngine(cfg, clk)
    # No on_submit(): see docstring — cold-start replay clears post_submit_active early.
    wakes_during_echo = 0
    for _, frame in replay_frames(_DELIVERY):
        obs = drv.observe(frame, ctx)
        clk.advance(1.0)
        for a in eng.tick(obs, ctx):
            if isinstance(a, Publish) and a.kind == "waiting_for_user":
                if obs.submitted_echo_present:
                    wakes_during_echo += 1
    assert wakes_during_echo == 0, (
        f"{wakes_during_echo} waiting_for_user publish(es) fired while submitted echo was "
        "visible in the active input box (submitted_echo_present=True)")
