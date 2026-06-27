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


# Real frames from session s-d719f7b9: this Claude Code build (v2.1.191 / glm) renders its working
# spinner WITHOUT "esc to interrupt" — an actively-working agent was misread as idle_prompt.
WORKING_SPINNER_STARTUP = (
    "...internal/engine/engine.go:813.\n"
    "✽ Cultivating…\n"
    "  ⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY takes precedence ove…\n"
    "❯ \n"
    "⏵⏵ auto mode on (shift+tab to cycle)\n")

WORKING_SPINNER_COUNTER = (
    "✽ Recombobulating… (1m 58s · ↓ 4.0k tokens)\n"
    "❯ Начинай реализацию. Читай спеку целиком.\n"
    "⏵⏵ auto mode on (shift+tab to cycle)\n")

WORKING_SPINNER_MIDDOT = (
    "·Recombobulating… still thinking with xhigh effort\n"
    "❯ \n"
    "⏵⏵ auto mode on (shift+tab to cycle)\n")


def test_working_spinner_without_interrupt_marker_is_working():
    # CLI drift: the spinner line carries no "esc to interrupt". A STABLE spinner frame with the ❯
    # box visible must classify working, NOT idle_prompt — that misread was the false-waiting_for_user
    # bug that made the orchestrator nudge a working agent.
    assert D.classify(WORKING_SPINNER_STARTUP, Ctx(stable_for=9.9)) == "working"
    assert D.classify(WORKING_SPINNER_COUNTER, Ctx(stable_for=9.9)) == "working"
    assert D.classify(WORKING_SPINNER_MIDDOT, Ctx(stable_for=9.9)) == "working"


def test_idle_and_menu_not_swallowed_by_working_marker():
    # The new positive working marker must NOT misread a genuine prompt as working.
    assert D.classify(INPUT_BOX, Ctx(stable_for=2.0)) == "idle_prompt"
    assert D.classify(PERMISSION, Ctx(stable_for=2.0)) == "permission_prompt"


def test_output_ellipsis_or_stray_glyph_is_not_working():
    # A genuine idle frame whose visible output merely ENDS with "…" (a truncated warning) or contains
    # a stray sparkle mid-line must stay idle_prompt. A false "working" here would HANG the orchestrator
    # (it would never be woken for the real prompt).
    warn_then_idle = (
        "  ⚠ claude.ai connectors are disabled because KEY takes precedence ove…\n"
        "❯ \n⏵⏵ auto mode on (shift+tab to cycle)\n")
    assert D.classify(warn_then_idle, Ctx(stable_for=2.0)) == "idle_prompt"
    stray_glyph = "  ✓ wrote file, ✻ done\n❯ \n⏵⏵ auto mode on (shift+tab to cycle)\n"
    assert D.classify(stray_glyph, Ctx(stable_for=2.0)) == "idle_prompt"


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


def test_format_submission_wraps_task_in_bracketed_paste():
    # nelix-10z: wrapping the task in bracketed-paste markers makes Claude collapse it to a
    # "[Pasted text #N]" placeholder with almost no re-render output (0.0s vs 2.2s raw echo for
    # 61.5KB). The markers frame ONLY the text — the submit key (Enter) is pressed separately,
    # OUTSIDE the paste, so a CR inside the markers can't end the paste early.
    assert D.format_submission("do the thing") == "\x1b[200~do the thing\x1b[201~"


def test_pasted_placeholder_only_on_active_input_line():
    # placeholder on the real (last) input line -> True
    box = "Welcome back!\n❯ [Pasted text #1]\n⏵⏵ ask mode (shift+tab to cycle)\n"
    assert D.input_submission_present(box, "a long collapsed task") is True
    # placeholder ONLY in scrolled agent output, with an empty real prompt below -> False
    out = ("agent log: ❯ [Pasted text #9] (quoted)\nmore output\n"
           "❯ \n⏵⏵ ask mode (shift+tab to cycle)\n")
    assert D.input_submission_present(out, "a long collapsed task") is False
