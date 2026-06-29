import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.drivers.claude import ClaudeDriver       # noqa: E402
from daemon.observation import ObservationCtx        # noqa: E402

D = ClaudeDriver()
CTX = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)


def test_working_spinner_is_busy():
    o = D.observe("✻Envisioning… (46s · ↓ 1.9k tokens)\n❯ \n⏵⏵ auto mode on (shift+tab to cycle)", CTX)
    assert o.prompt_kind == "none"                       # busy
    assert o.heartbeat.present is True
    assert o.heartbeat.expected_to_change is True
    assert o.heartbeat.fp is not None


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


def test_empty_prompt_is_free_text():
    o = D.observe("Welcome back!\n❯ \n⏵⏵ auto mode on (shift+tab to cycle)", CTX)
    assert o.prompt_kind == "free_text"
    assert "accepts_text_input" in o.affordances


def test_stray_prompt_marker_without_footer_is_not_free_text():
    # BLOCKER 1: a ❯ in scrolled output / chrome WITHOUT the real prompt footer must NOT be a
    # free-text input box — delivery must never type into a non-input screen that merely contains ❯.
    frame = "agent log: ❯ see the arrow in this output line\nmore output here\n(no mode footer)"
    o = D.observe(frame, CTX)
    assert o.prompt_kind == "unknown"
    assert "accepts_text_input" not in o.affordances


def test_numbered_menu_is_modal_choice_with_options():
    frame = ("How should T7 handle the table?\n❯ 1. Enrich all three\n  2. Verify-only\n"
             "  3. Enrich establish_phase only\nEnter to select · ↑/↓ to navigate")
    o = D.observe(frame, CTX)
    assert o.prompt_kind == "modal_choice"               # NOT free_text (fixes F2)
    assert [x.id for x in o.options] == ["1", "2", "3"]
    assert o.options[0].label == "Enrich all three"
    assert "modal_choice" in o.affordances


def test_yes_no_menu_is_permission_choice():
    frame = ("Do you want to make this edit?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No\n")
    o = D.observe(frame, CTX)
    assert o.prompt_kind == "permission_choice"
    assert [x.id for x in o.options] == ["1", "2", "3"]
    assert "permission_choice" in o.affordances


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


def test_ask_mode_reflected():
    ask = D.observe("Welcome\n❯ \n⏵⏵ ask mode (shift+tab to cycle)", CTX)
    assert ask.ask_mode is True
    auto = D.observe("Welcome\n❯ \n⏵⏵ accept edits on (shift+tab to cycle)", CTX)
    assert auto.ask_mode is False


def test_fingerprints_split_content_from_input():
    # content_fp excludes the active input row: our echo in the box must not move content_fp.
    a = D.observe("agent output line\n❯ \n⏵⏵ ask mode (shift+tab to cycle)", CTX)
    b = D.observe("agent output line\n❯ some typed text\n⏵⏵ ask mode (shift+tab to cycle)", CTX)
    assert a.content_fp == b.content_fp                   # input region change ignored
    # but a real output change moves content_fp
    c = D.observe("DIFFERENT output\n❯ \n⏵⏵ ask mode (shift+tab to cycle)", CTX)
    assert a.content_fp != c.content_fp


def test_busy_reason_from_chrome_only():
    o = D.observe("⏺ Bash(go test ./...)\n  esc to interrupt", CTX)
    assert o.busy_reason == "running_command"
    plain = D.observe("✻ Thinking…", CTX)
    assert plain.busy_reason is None                      # no chrome marker -> None


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
