import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.drivers.claude import ClaudeDriver  # noqa: E402


class Ctx:
    def __init__(self, stable_for=9.9, bytes_idle_for=9.9, child_alive=True, exit_code=None):
        self.stable_for = stable_for; self.bytes_idle_for = bytes_idle_for
        self.child_alive = child_alive; self.exit_code = exit_code


D = ClaudeDriver()


def test_working_when_interrupt_marker():
    assert D.classify("doing things… esc to interrupt", Ctx(stable_for=9.9)) == "working"


def test_idle_prompt_only_when_stable():
    frame = "Here is my answer.\n❯ "
    assert D.classify(frame, Ctx(stable_for=0.2)) == "quiet_working"   # box present but not settled
    assert D.classify(frame, Ctx(stable_for=2.0)) == "idle_prompt"     # settled -> stop


def test_permission_prompt():
    frame = "Proceed?\n 1. Yes\n 3. No\n❯ "
    assert D.classify(frame, Ctx(stable_for=2.0)) == "permission_prompt"


def test_crashed_and_exit_code():
    assert D.classify("Traceback (most recent call last):", Ctx()) == "crashed"
    assert D.classify("anything", Ctx(child_alive=False, exit_code=0)) == "exited"
    assert D.classify("anything", Ctx(child_alive=False, exit_code=2)) == "crashed"


def test_quiet_working_when_alive_no_markers():
    assert D.classify("compiling…", Ctx(stable_for=0.1)) == "quiet_working"


def test_normalize_frame_zeroes_spinner():
    a = D.normalize_frame("⠋ thinking 1.2s · 3 tokens\n❯ ")
    b = D.normalize_frame("⠙ thinking 4.8s · 9 tokens\n❯ ")
    assert a == b   # spinner/clock/counter differences erased -> semantically stable


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

WORKING = "doing things… esc to interrupt\n"


def test_is_modal_choice_matches_two_and_three_option_menus():
    assert D.is_modal_choice(TRUST) is True
    assert D.is_modal_choice(PERMISSION) is True
    assert D.is_modal_choice(INPUT_BOX) is False
    assert D.is_modal_choice(WORKING) is False


def test_is_accepting_input_only_at_real_prompt():
    assert D.is_accepting_input(INPUT_BOX) is True
    assert D.is_accepting_input(TRUST) is False       # menu, not input
    assert D.is_accepting_input(PERMISSION) is False
    assert D.is_accepting_input(WORKING) is False


def test_input_submission_present_detects_typed_or_pasted_task():
    typed = INPUT_BOX.replace("❯ \n", "❯ create report.md with a header\n")
    assert D.input_submission_present(typed, "create report.md with a header") is True
    assert D.input_submission_present(INPUT_BOX, "create report.md with a header") is False
    # Claude collapses a long/multiline task into a placeholder ON the prompt line (ASCII space after ❯).
    pasted = INPUT_BOX.replace("❯ \n", "❯ [Pasted text #1]\n")
    assert D.input_submission_present(pasted, "a long task that claude collapsed") is True
    # Claude actually separates ❯ and the placeholder with a NBSP (U+00A0) — must match.
    nbsp_pasted = INPUT_BOX.replace("❯ \n", "❯ [Pasted text #1]\n")
    assert D.input_submission_present(nbsp_pasted, "a long task that claude collapsed") is True
    # A placeholder echoed in OUTPUT while the prompt is empty must NOT count as our submission.
    output_only = "log: [Pasted text #1] was mentioned\n" + INPUT_BOX
    assert D.input_submission_present(output_only, "a long task that claude collapsed") is False


def test_pasted_placeholder_only_on_active_input_line():
    # placeholder on the real (last) input line -> True
    box = "Welcome back!\n❯ [Pasted text #1]\n⏵⏵ ask mode (shift+tab to cycle)\n"
    assert D.input_submission_present(box, "a long collapsed task") is True
    # placeholder ONLY in scrolled agent output, with an empty real prompt below -> False
    out = ("agent log: ❯ [Pasted text #9] (quoted)\nmore output\n"
           "❯ \n⏵⏵ ask mode (shift+tab to cycle)\n")
    assert D.input_submission_present(out, "a long collapsed task") is False
