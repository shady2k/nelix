import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.drivers.claude import ClaudeDriver        # noqa: E402
from daemon.observation import ObservationCtx         # noqa: E402

D = ClaudeDriver()


def _ctx(text=None):
    return ObservationCtx(last_submitted_text=text, child_alive=True, exit_code=None)


# ---- retained methods (not classification: framing / transcript hygiene) ----

def test_normalize_frame_zeroes_spinner():
    a = D.normalize_frame("⠋ thinking 1.2s · 3 tokens\n❯ ")
    b = D.normalize_frame("⠙ thinking 4.8s · 9 tokens\n❯ ")
    assert a == b   # spinner/clock/counter differences erased -> semantically stable


def test_format_submission_wraps_task_in_bracketed_paste():
    # Wrapping the task in bracketed-paste markers makes Claude collapse it to a "[Pasted text #N]"
    # placeholder with almost no re-render. The markers frame ONLY the text — the submit key (Enter)
    # is pressed separately by the session, OUTSIDE the paste.
    assert D.format_submission("do the thing") == "\x1b[200~do the thing\x1b[201~"


def test_is_transcript_volatile_anchored():
    assert D.is_transcript_volatile("✽ Recombobulating… (1m 58s · ↓ 4.0k tokens)") is True
    assert D.is_transcript_volatile("doing things esc to interrupt") is True
    assert D.is_transcript_volatile("⏵⏵ auto mode on (shift+tab to cycle)") is True
    assert D.is_transcript_volatile("❯ some input") is True
    assert D.is_transcript_volatile("Committed the change to internal/conn") is False


# ---- observe-surfaced reads (the folded predicates' behavior) ----

TRUST = (
    "╭─ Claude Code ─╮\n"
    "Quick safety check: Is this a project you created or one you trust?\n"
    "❯ 1. Yes, I trust this folder\n"
    "  2. No, exit\n"
    "Enter to confirm · Esc to cancel\n")

PERMISSION = (
    "Do you want to make this edit?\n"
    "❯ 1. Yes\n"
    "  2. Yes, and don't ask again\n"
    "  3. No\n")

INPUT_BOX = (
    "Welcome back!\n"
    "❯ \n"
    "⏵⏵ auto mode on (shift+tab to cycle)\n")


def test_modal_menus_surface_as_choices_input_box_does_not():
    assert D.observe(TRUST, _ctx()).prompt_kind == "modal_choice"        # numbered, not Yes/No gate
    assert D.observe(PERMISSION, _ctx()).prompt_kind == "permission_choice"
    assert D.observe(INPUT_BOX, _ctx()).prompt_kind == "free_text"


def test_input_box_is_only_free_text_not_a_menu():
    assert "accepts_text_input" in D.observe(INPUT_BOX, _ctx()).affordances
    assert "accepts_text_input" not in D.observe(TRUST, _ctx()).affordances
    assert "accepts_text_input" not in D.observe(PERMISSION, _ctx()).affordances


def test_submitted_echo_detects_typed_or_pasted_task():
    typed = INPUT_BOX.replace("❯ \n", "❯ create report.md with a header\n")
    assert D.observe(typed, _ctx("create report.md with a header")).submitted_echo_present is True
    assert D.observe(INPUT_BOX, _ctx("create report.md with a header")).submitted_echo_present is False
    # Claude collapses a long/multiline task into a placeholder ON the prompt line.
    pasted = INPUT_BOX.replace("❯ \n", "❯ [Pasted text #1]\n")
    assert D.observe(pasted, _ctx("a long task that claude collapsed")).submitted_echo_present is True
    # A placeholder echoed in OUTPUT while the prompt is empty must NOT count as our submission.
    output_only = "log: [Pasted text #1] was mentioned\n" + INPUT_BOX
    assert D.observe(output_only, _ctx("a long task that claude collapsed")).submitted_echo_present is False


