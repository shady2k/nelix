import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.drivers.claude import ClaudeDriver       # noqa: E402
from daemon.observation import ObservationCtx        # noqa: E402

D = ClaudeDriver()
CTX = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)



def test_working_heartbeat_fp_tracks_animation():
    a = D.observe("✻ Recombobulating… (58s · 4 tokens)\n❯ ", CTX)
    b = D.observe("✻ Recombobulating… (59s · 9 tokens)\n❯ ", CTX)
    assert a.heartbeat.fp != b.heartbeat.fp              # spinner/timer animation -> live
    # but the semantic fingerprint (elapsed/tokens zeroed) is stable
    assert a.semantic_fp == b.semantic_fp


def test_interrupt_marker_is_busy_and_affords_interrupt():
    o = D.observe("doing things… esc to interrupt", CTX)
    assert o.prompt_kind == "none"
    assert "interrupt_available" in o.affordances




def test_submitted_echo_detected():
    ctx = ObservationCtx(last_submitted_text="commit this", child_alive=True, exit_code=None)
    o = D.observe("done\n❯ commit this\n⏵⏵ auto mode on (shift+tab to cycle)", ctx)
    assert o.submitted_echo_present is True               # post-submit gap signal (fixes F1)


def test_no_echo_when_text_absent():
    ctx = ObservationCtx(last_submitted_text="commit this", child_alive=True, exit_code=None)
    o = D.observe("done\n❯ \n⏵⏵ auto mode on (shift+tab to cycle)", ctx)
    assert o.submitted_echo_present is False


def test_echo_only_in_scrollback_is_not_present():
    # BLOCKER 2: our submitted text appearing in agent OUTPUT/scrollback (not the active input box)
    # must NOT count as an echo — else it suppresses real prompts forever (spec §5.5/§10).
    ctx = ObservationCtx(last_submitted_text="commit this", child_alive=True, exit_code=None)
    scroll = ("agent output mentions commit this in a log line\n"
              "❯ \n⏵⏵ ask mode (shift+tab to cycle)")
    assert D.observe(scroll, ctx).submitted_echo_present is False
    # but the SAME text on the active prompt line (verbatim) IS an echo
    active = "done\n❯ commit this\n⏵⏵ ask mode (shift+tab to cycle)"
    assert D.observe(active, ctx).submitted_echo_present is True


def test_crash_and_exit_from_ctx():
    assert D.observe("anything", ObservationCtx(None, False, 0)).prompt_kind == "exit"
    assert D.observe("anything", ObservationCtx(None, False, 2)).prompt_kind == "crash"


def test_crash_banner_in_frame():
    o = D.observe("Traceback (most recent call last):", CTX)
    assert o.prompt_kind == "crash"


def test_fingerprints_split_content_from_input():
    # content_fp excludes the active input row: our echo in the box must not move content_fp.
    a = D.observe("agent output line\n❯ \n⏵⏵ ask mode (shift+tab to cycle)", CTX)
    b = D.observe("agent output line\n❯ some typed text\n⏵⏵ ask mode (shift+tab to cycle)", CTX)
    assert a.content_fp == b.content_fp                   # input region change ignored
    # but a real output change moves content_fp
    c = D.observe("DIFFERENT output\n❯ \n⏵⏵ ask mode (shift+tab to cycle)", CTX)
    assert a.content_fp != c.content_fp



# ---- actuation contract (driver owns the keys; Session writes) -------------------------

def test_format_submission_wraps_in_bracketed_paste():
    assert D.format_submission("do the thing") == "\x1b[200~do the thing\x1b[201~"


def test_select_option_presses_digit_and_submits():
    assert D.select_option("2") == "2\r"


def test_submit_text_is_raw_answer():
    assert D.submit_text("hello") == "hello"


def test_interrupt_is_escape():
    assert D.interrupt() == "\x1b"


def test_classify_and_folded_predicates_are_gone():
    for gone in ("classify", "is_accepting_input", "is_modal_choice", "is_ask_mode",
                 "input_submission_present"):
        assert not hasattr(D, gone), f"{gone} must be removed (folded into observe)"


def test_ask_mode_read_path_removed():
    # nelix-zl9: the daemon is a dumb bridge — the whole ask-mode read path is deleted.
    from daemon.observation import Observation
    from daemon.drivers.claude import ClaudeDriver
    from daemon.drivers.base import Driver
    assert "ask_mode" not in Observation.__dataclass_fields__
    assert not hasattr(ClaudeDriver, "ask_mode_toggle")
    assert not hasattr(ClaudeDriver, "_ask_mode")
    assert not hasattr(Driver, "ask_mode_toggle")


# ---- background subagent: a running subagent is BUSY, never waiting_for_user ----
# While a background subagent runs, Claude keeps the input box live (❯ + footer) AND shows
# "✻ Waiting for N background agent(s) to finish". That empty-looking box is NOT a genuine
# free-text prompt — the main turn is blocked on the subagent. (Real-capture: s-039a61b4.)
_BG_FRAME = (
    "⏺ The implementer is running in the background. I'll report when it's done.\n"
    "\n"
    "✻ Waiting for 1 background agent to finish\n"
    "────────────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────────────\n"
    "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents · ↓ to manage\n"
    "  ⏺ main\n"
    "  ◯ golang-pro  Implement T8 stats ticker wiring          33s · ↓ 33.8k tokens"
)



def test_background_subagent_ticker_does_not_churn_semantic_fp():
    # The subagent's live "<elapsed> · ↓ <N>k tokens" ticker must be normalized away, else every
    # tick re-mints the fingerprint and defeats the engine's anti-flap (spec §7.2).
    a = D.observe(_BG_FRAME, CTX)
    b = D.observe(_BG_FRAME.replace("33s · ↓ 33.8k tokens", "41s · ↓ 90.8k tokens"), CTX)
    assert a.semantic_fp == b.semantic_fp


def test_modal_prompt_during_background_subagent_still_surfaces():
    # Guard: a REAL prompt (permission/modal) that co-occurs with a running subagent must NOT be
    # masked by the busy-subagent read — the orchestrator must still be woken for the decision.
    frame = ("Do you want to make this edit?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No\n"
             "✻ Waiting for 1 background agent to finish\n"
             "  ◯ golang-pro  Implement T8 stats ticker wiring          33s · ↓ 33.8k tokens")
    o = D.observe(frame, CTX)
    assert o.prompt_kind == "permission_choice"
    assert [x.id for x in o.options] == ["1", "2", "3"]
