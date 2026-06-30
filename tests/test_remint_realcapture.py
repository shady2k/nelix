"""Real-capture regression: a published respondable prompt must NOT be re-minted while the agent
sits UNANSWERED at the same prompt region (the orchestrator "double-ask").

Replays an actual captured session (`s-2190cfb2`, Claude Code v2.1.x) where Claude waited at a
free-text prompt for the user to answer ONE pending question. While it waited, the rendered frame
churned: the TUI repaints the scrolled conversation region row-by-row, so each byte chunk lands
mid-repaint and the whole-frame `semantic_fp` changes every tick while the bottom-anchored `❯` box
(`prompt_fp`) stays stable. The old BeliefEngine keyed each decision on `semantic_fp`, so every
churn minted a fresh `waiting_for_user` wake (a new decision_id) and the orchestrator re-asked the
user the SAME question ~25x. The cure is a belief-level backstop: once a respondable prompt is
published, do NOT re-publish until the prompt region changes / goes busy / is answered.

The capture is fed from byte 0 (the session enters the alternate screen at startup and repaints by
cursor-home with no full clears, so terminal state — incl. the persistent input box + footer — is
cumulative and cannot be front-trimmed; only the unused tail past the second prompt is dropped).

Drives the REAL renderer over the REAL bytes (fabricated frames missed exactly this class of bug).
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.renderer.ghostty import GhosttyRenderer   # noqa: E402
from daemon.drivers.claude import ClaudeDriver         # noqa: E402
from daemon.observation import ObservationCtx          # noqa: E402
from daemon.belief import BeliefEngine, Publish         # noqa: E402
from daemon.clock import FakeClock                      # noqa: E402
from daemon.config import BeliefConfig                  # noqa: E402

_CAPTURE = Path(__file__).parent / "golden" / "claude" / "_regression" / "s-2190cfb2-remint.raw"
_CTX = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)


def _replay_publishes():
    """Drive the real renderer+driver+engine over the capture; return (pubs, sfps, order) where
    `pubs[prompt_fp]` = number of waiting_for_user publishes that fired while that prompt region
    was on screen, `sfps[prompt_fp]` = the distinct semantic_fp seen at that free_text prompt
    (used to locate the churn window from the data, without hardcoding a fingerprint), and `order`
    = the prompt_fp of every waiting_for_user publish in firing order."""
    raw = _CAPTURE.read_bytes()
    drv = ClaudeDriver()
    r = GhosttyRenderer(120, 40)
    clk = FakeClock(0.0)
    eng = BeliefEngine(BeliefConfig(), clk)
    pubs = defaultdict(int)
    sfps = defaultdict(set)
    order = []
    seen = None
    for i in range(0, len(raw), 256):
        r.feed(raw[i:i + 256])
        frame = r.render()
        if frame == seen:
            continue
        seen = frame
        obs = drv.observe(frame, _CTX)
        clk.advance(1.0)               # > idle_confirm_window, so a stable prompt settles & publishes
        if obs.prompt_kind == "free_text":
            sfps[obs.prompt_fp].add(obs.semantic_fp)
        for a in eng.tick(obs, _CTX):
            if isinstance(a, Publish) and a.kind == "waiting_for_user":
                pubs[obs.prompt_fp] += 1
                order.append(obs.prompt_fp)
    return pubs, sfps, order


def test_stable_prompt_churn_publishes_waiting_for_user_once():
    pubs, sfps, _ = _replay_publishes()
    # The churn window = the free_text prompt region that carried the most distinct semantic_fp
    # (the bottom-anchored ❯ box held stable while the repaint churned the frame meaning).
    churn_fp, churn_sfps = max(sfps.items(), key=lambda kv: len(kv[1]))
    assert len(churn_sfps) >= 10, (
        f"fixture should exercise the stable-prompt churn (saw {len(churn_sfps)} semantic states)")
    # RED before the fix: ~25 publishes (one per semantic churn). GREEN after: exactly one.
    assert pubs[churn_fp] == 1, (
        f"{pubs[churn_fp]} waiting_for_user wakes for ONE unanswered prompt whose region never changed")


def test_a_genuinely_new_prompt_still_publishes_after_the_churn():
    # Faithfulness guard: the backstop suppresses re-minting at a HELD prompt — it must NOT swallow a
    # real new question. Assert ORDER, not just presence: AFTER the churn prompt publishes (once),
    # a genuinely different prompt region (different prompt_fp) appears LATER in the capture and
    # still publishes its own waiting_for_user. This fails if the fix over-suppresses (swallows the
    # later prompt) — a `>=2 distinct fps published` check could not tell that apart.
    pubs, sfps, order = _replay_publishes()
    churn_fp, _ = max(sfps.items(), key=lambda kv: len(kv[1]))
    assert churn_fp in order, f"churn prompt never published; got {order!r}"
    after_churn = order[order.index(churn_fp) + 1:]
    assert any(fp != churn_fp for fp in after_churn), (
        f"no fresh publish at a different prompt_fp after the churn prompt; publish order={order!r}")
