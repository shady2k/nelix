import re

from daemon.drivers import register

WORKING_MARKERS = ("esc to interrupt",)
CRASH_MARKERS = ("Traceback (most recent call last)", "command not found",
                 "Invalid API key", "authentication_error")
INPUT_BOX_MARKERS = ("❯",)
_AUTO_MODE_MARKERS = ("accept edits on", "plan mode on", "bypass permissions")

# Volatile regions to zero out before measuring semantic stability:
#   braille spinners, "<n.n>s", "<n> tokens", standalone changing counters.
_SPINNER = re.compile(r"[⠀-⣿]")
_ELAPSED = re.compile(r"\d+(?:\.\d+)?s\b")
_TOKENS = re.compile(r"\d+\s+tokens?\b")


def _is_choice_prompt(frame):
    # When Claude needs a decision it presents a numbered Yes/…/No selection menu; the
    # Yes+No options are the stable signal across prompt types (headers differ).
    return "1. Yes" in frame and "3. No" in frame


@register("claude")
class ClaudeDriver:
    ask_mode_toggle = "\x1b[Z"  # Shift+Tab cycles the permission mode in the TUI

    def normalize_frame(self, frame):
        f = _SPINNER.sub("", frame)
        f = _ELAPSED.sub("Ns", f)
        f = _TOKENS.sub("N tokens", f)
        return f

    def classify(self, frame, ctx):
        if not ctx.child_alive:
            return "exited" if (ctx.exit_code or 0) == 0 else "crashed"
        if any(m in frame for m in CRASH_MARKERS):
            return "crashed"
        if any(m in frame for m in WORKING_MARKERS):
            return "working"
        at_input = any(m in frame for m in INPUT_BOX_MARKERS)
        if at_input and ctx.stable_for >= self._settle:
            return "permission_prompt" if _is_choice_prompt(frame) else "idle_prompt"
        return "quiet_working"

    # settle threshold is injected by the session from config before classify is used
    _settle = 1.5

    def is_ask_mode(self, frame):
        # Ask-mode = a known mode line is present but none of the auto markers are.
        if "shift+tab to cycle" not in frame:
            return False
        return not any(m in frame for m in _AUTO_MODE_MARKERS)
