"""Tier-2 sequence oracles — behaviour across the REAL frame STREAM.

Each oracle replays a committed regression raw through the shared helper, the real
ClaudeDriver, and (for belief tests) the real BeliefEngine, asserting an invariant that
spans the full sequence of observations rather than a single frame.

I2b — bg-subagent session never publishes waiting_for_user            (regression: cd3352d)
I8  — post-submit echo window suppressed (no false-idle publish)       (regression: 6de482c)
I4b — submitted echo detected only in the ACTIVE input region          (regression: 68d6c7c)

Real captures (tests/golden/claude/_regression/):
  s-039a61b4-bg-subagent.raw  — bg subagent running (I2b)
  s-b8a30317-delivery.raw     — paste delivery, echo visible post-submit (I8, I4b+)
  s-2190cfb2-remint.raw       — free-text prompt churn (I4b-)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._replay import replay_frames, replay_observations   # noqa: E402
from daemon.drivers.claude import ClaudeDriver                 # noqa: E402
from daemon.observation import ObservationCtx                  # noqa: E402
from daemon.belief import BeliefEngine, Publish                # noqa: E402
from daemon.clock import FakeClock                             # noqa: E402
from daemon.config import BeliefConfig                         # noqa: E402

_GOLDEN   = Path(__file__).parent / "golden" / "claude" / "_regression"
_BG       = (_GOLDEN / "s-039a61b4-bg-subagent.raw").read_bytes()
_DELIVERY = (_GOLDEN / "s-b8a30317-delivery.raw").read_bytes()
_REMINT   = (_GOLDEN / "s-2190cfb2-remint.raw").read_bytes()

_CTX_PLAIN = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)


# ─────────────────────────────────────────────────────────────────────────────
# I2b — bg-subagent session never publishes waiting_for_user
# ─────────────────────────────────────────────────────────────────────────────

def test_bg_subagent_never_publishes_waiting_for_user():
    """While a background subagent runs, the screen is busy — the engine must NOT tell the
    orchestrator the user is needed.  Regression: cd3352d (bg frames classified free_text →
    engine published waiting_for_user while the turn was blocked on a golang-pro subagent).

    RED command (pre-fix historical checkout — cd3352d~1 is the actual broken code):
        git show cd3352d~1:daemon/drivers/claude.py > daemon/drivers/claude.py
        .venv/bin/python -m pytest tests/test_sequence_oracles.py::test_bg_subagent_never_publishes_waiting_for_user -v
    RED result: 61 waiting_for_user publishes → assert 61 == 0 → FAIL.
    Restore: git checkout daemon/drivers/claude.py.

    GREEN: 0 waiting_for_user publishes across the full 570 kB replay.

    Non-vacuity guard: assert >100 frames have busy_reason='waiting_subagents' before
    the zero-publishes assertion — if the fixture is wrong or the driver has regressed
    to never seeing a bg window, the guard fails rather than the test passing 0==0.
    Observed in real replay: ~1002 waiting_subagents frames out of 2076 total.
    """
    cfg = BeliefConfig()
    drv, clk = ClaudeDriver(), FakeClock(0.0)
    eng = BeliefEngine(cfg, clk)
    n_waiting_publishes = 0
    bg_frames = 0
    for _, frame in replay_frames(_BG):
        obs = drv.observe(frame, _CTX_PLAIN)
        clk.advance(1.0)
        if obs.busy_reason == "waiting_subagents":
            bg_frames += 1
        for a in eng.tick(obs, _CTX_PLAIN):
            if isinstance(a, Publish) and a.kind == "waiting_for_user":
                n_waiting_publishes += 1
    assert bg_frames > 100, (
        f"bg-subagent replay must contain >100 frames with busy_reason='waiting_subagents'; "
        f"saw {bg_frames} — fixture may be wrong or driver classification has regressed")
    assert n_waiting_publishes == 0, (
        f"{n_waiting_publishes} waiting_for_user publish(es) fired during bg-subagent session "
        f"(bg_frames={bg_frames})")


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
    echo_frames = 0          # frames where submitted_echo_present=True
    echo_free_text_frames = 0  # echo-visible frames that are also prompt_kind=free_text
    for _, frame in replay_frames(_DELIVERY):
        obs = drv.observe(frame, ctx)
        clk.advance(1.0)
        if obs.submitted_echo_present:
            echo_frames += 1
            if obs.prompt_kind == "free_text":
                echo_free_text_frames += 1
        for a in eng.tick(obs, ctx):
            if isinstance(a, Publish) and a.kind == "waiting_for_user":
                if obs.submitted_echo_present:
                    wakes_during_echo += 1
    # Non-vacuity guards: assert the echo window was actually observed before claiming
    # zero wakes.  Without these, if echo detection regresses to always-False the test
    # passes 0==0 while guarding nothing.
    # Observed in real replay: echo_frames=2, echo_free_text_frames=1.
    assert echo_frames > 0, (
        "delivery capture must have ≥1 frame with submitted_echo_present=True "
        "(echo detection may have regressed to always-False; saw echo_frames=0)")
    assert echo_free_text_frames > 0, (
        "delivery capture must have ≥1 echo-visible free_text frame "
        "(the prompt phase where suppression must fire; saw echo_free_text_frames=0)")
    assert wakes_during_echo == 0, (
        f"{wakes_during_echo} waiting_for_user publish(es) fired while submitted echo was "
        "visible in the active input box (submitted_echo_present=True)")


# ─────────────────────────────────────────────────────────────────────────────
# I4b — submitted echo detected only in the ACTIVE input region
# ─────────────────────────────────────────────────────────────────────────────

def test_echo_in_active_box_is_detected():
    """Positive half (I4b): when the submitted text's placeholder appears in the ACTIVE
    input tail (last ❯ onward), submitted_echo_present is True.

    Source: s-b8a30317-delivery — frames at offsets 1792 and 2048 carry the placeholder
    '❯\\xa0[Pasted text #1]' in the active tail; _PASTED_TEXT must match there.

    RED (same mutation as negative half — both halves share the 68d6c7c bug trigger):
        In ClaudeDriver._echo_present(), change
            tail = frame[frame.rfind("❯"):] if "❯" in frame else ""
        to
            tail = frame
        The positive assertion still passes (text IS in the tail ⊂ whole frame), but
        test_echo_in_scrollback_not_detected FAILS — demonstrating the bug.
    """
    ctx = ObservationCtx(last_submitted_text="a task that was pasted", child_alive=True, exit_code=None)
    found = any(obs.submitted_echo_present for _, obs in replay_observations(_DELIVERY, ctx))
    assert found, (
        "delivery capture must have ≥1 frame with submitted_echo_present=True "
        "(paste placeholder '❯\\xa0[Pasted text #1]' expected in active tail)")


def test_echo_in_scrollback_not_detected():
    """Negative half (I4b): text appearing ONLY in the scrollback (not in the active tail)
    must NOT trigger submitted_echo_present=True.  Regression: 68d6c7c — echo detection was
    not scoped to the active tail, so a user's prior submission visible in scrollback was
    misread as still-in-the-box.

    Source: s-2190cfb2-remint — at offset 396032 the phrase 'Checking for updates' appears
    in the scrollback (conversation history) but the active tail is just '❯\\n──…'.
    We scan frames at offset > 7000 to skip the capture's own paste-delivery window
    (offsets 1792–6400 carry [Pasted text #1] in the active tail), isolating the
    scrollback-only case.

    RED mutation (local, NOT committed):
        In daemon/drivers/claude.py, in ClaudeDriver._echo_present(), change:
            tail = frame[frame.rfind("❯"):] if "❯" in frame else ""
        to:
            tail = frame
        Then run:
            .venv/bin/python -m pytest tests/test_sequence_oracles.py::test_echo_in_scrollback_not_detected -v
    RED result: 'Checking for updates' found via needle search in the full frame at
    offset 396032 → echo_frames=1 → assert echo_frames==0 → FAIL.
    Restore: git checkout daemon/drivers/claude.py.

    GREEN: 0 late frames with submitted_echo_present=True.

    # DOCUMENTED GAP (corpus lacks a scrollback-only negative for the _PASTED_TEXT path;
    # the remint capture starts with a paste-delivery phase at offsets 1792–6400 that also
    # places [Pasted text #1] in the active tail; the text-needle path IS covered by the
    # 'Checking for updates' frame at offset 396032; see INVENTORY.md I4b row).
    """
    ctx = ObservationCtx(last_submitted_text="Checking for updates", child_alive=True, exit_code=None)
    drv = ClaudeDriver()

    scrollback_only_frames = 0   # text in scrollback but NOT in active tail (makes test non-trivial)
    echo_frames = 0              # frames where submitted_echo_present is incorrectly True

    for off, frame in replay_frames(_REMINT):
        if off <= 7000:   # skip the capture's paste-delivery window (offsets 1792–6400)
            continue
        tail_start = frame.rfind("❯")
        tail = frame[tail_start:] if tail_start >= 0 else ""
        scrollback = frame[:tail_start] if tail_start >= 0 else frame
        if "Checking for updat" in scrollback and "Checking for updat" not in tail:
            scrollback_only_frames += 1
        obs = drv.observe(frame, ctx)
        if obs.submitted_echo_present:
            echo_frames += 1

    assert scrollback_only_frames >= 1, (
        "fixture must have ≥1 late frame with 'Checking for updates' in scrollback "
        "(makes the negative test non-trivial: text IS in session, just not in active tail)")
    assert echo_frames == 0, (
        f"{echo_frames} late remint frames erroneously report submitted_echo_present=True "
        f"(text 'Checking for updates' leaked from scrollback into active-tail detection)")
