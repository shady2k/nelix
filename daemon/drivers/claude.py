from daemon.drivers import register

ASK_MODE_TOGGLE = "\x1b[Z"  # Shift+Tab cycles the permission mode in the TUI
# We are in ask-mode when the mode line is NOT an auto-accept / plan mode.
_AUTO_MODE_MARKERS = ("accept edits on", "plan mode on", "bypass permissions")

# Markers confirmed against real pyte-rendered frames of Claude Code v2.1.186 (spike P0-B).
WORKING_MARKERS = ("esc to interrupt",)
CRASH_MARKERS = ("Traceback (most recent call last)", "command not found",
                 "Invalid API key", "authentication_error")
INPUT_BOX_MARKERS = ("❯",)


def _is_choice_prompt(grid):
    # When Claude needs a decision (permission for an edit, a bash command, …) it presents
    # a numbered Yes/…/No selection menu. The Yes+No options are the decision-point signal,
    # stable across prompt types (the headers differ: "Do you want to proceed?" for bash,
    # "Create file …?" for edits, etc.).
    return "1. Yes" in grid and "3. No" in grid


@register("claude")
class ClaudeDriver:
    def is_task_accepted_signal(self, grid):
        return any(m in grid for m in WORKING_MARKERS)

    def classify(self, grid, task_accepted):
        if any(m in grid for m in CRASH_MARKERS):
            return "crashed"
        if _is_choice_prompt(grid):
            return "waiting_for_user"
        if any(m in grid for m in WORKING_MARKERS):
            return "working"
        if any(m in grid for m in INPUT_BOX_MARKERS):
            return "done_candidate" if task_accepted else "idle"
        return "idle"

    def is_ask_mode(self, grid):
        # Ask-mode = a known mode line is present but none of the auto markers are.
        if "shift+tab to cycle" not in grid:
            return False
        return not any(m in grid for m in _AUTO_MODE_MARKERS)
