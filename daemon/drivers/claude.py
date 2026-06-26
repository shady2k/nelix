import re

from daemon.drivers import register

WORKING_MARKERS = ("esc to interrupt",)
# Positive working signal for builds that DON'T keep "esc to interrupt" on screen (CLI drift):
# Claude's live status line is a spinner glyph at line-start + a status word + an ellipsis, e.g.
# "✽ Cultivating…" or "·Recombobulating… (1m 58s · ↓ 4.0k tokens)". The (elapsed·tokens) telemetry
# is OPTIONAL (most frames omit it). Anchored at line-start with a Capitalised word so a stray glyph
# or a "…" inside ordinary output (e.g. a truncated warning) can't trip it — a false "working" would
# hang the orchestrator (never woken for the real prompt), so this stays tight.
_WORKING_STATUS = re.compile(r"(?m)^[ \t]*[·✢✳✶✻✽✺✦][ \t]*[A-Z][A-Za-z]+.*?(?:…|\.\.\.)")
CRASH_MARKERS = ("Traceback (most recent call last)", "command not found",
                 "Invalid API key", "authentication_error")
INPUT_BOX_MARKERS = ("❯",)
_AUTO_MODE_MARKERS = ("accept edits on", "plan mode on", "bypass permissions")

# Volatile regions to zero out before measuring semantic stability:
#   braille spinners, "<n.n>s", "<n> tokens", standalone changing counters.
_SPINNER = re.compile(r"[⠀-⣿]")
_ELAPSED = re.compile(r"\d+(?:\.\d+)?s\b")
_TOKENS = re.compile(r"\d+\s+tokens?\b")

_PROMPT_FOOTER = "shift+tab to cycle"     # present at the interactive input prompt (any mode)
_OPTION = re.compile(r"^\s*[❯>]?\s*\d+\.\s+\S", re.M)        # a numbered menu option line
_SELECTED_OPTION = re.compile(r"^\s*❯\s*\d+\.\s+\S", re.M)   # the cursor sits ON an option
# Claude collapses long/multiline input into "[Pasted text #N]" ON the prompt line; the prompt marker
# (❯) sits immediately before it (Claude renders a NBSP between them). The character class matches
# space, tab, or NBSP (U+00A0) — note: " " must be in a non-raw string; it is NOT a raw string.
_PASTED_TEXT = re.compile("❯[ \t ]*" + r"\[Pasted text #\d+\]")


def _is_choice_prompt(frame):
    # When Claude needs a decision it presents a numbered Yes/…/No selection menu; the
    # Yes+No options are the stable signal across prompt types (headers differ).
    return "1. Yes" in frame and "3. No" in frame


@register("claude")
class ClaudeDriver:
    ask_mode_toggle = "\x1b[Z"  # Shift+Tab cycles the permission mode in the TUI
    command_prefixes = ("/",)   # a leading '/' opens a TUI slash-command, not a prompt
    submit_key = "\r"           # the TUI treats CR (not LF) as Enter

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
        # Positive working detection BEFORE the input-box/settle idle path: the ❯ box stays visible
        # while the agent works, so "stable + ❯" alone is not idle. Legacy marker OR the spinner line.
        if any(m in frame for m in WORKING_MARKERS) or _WORKING_STATUS.search(frame):
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

    def is_modal_choice(self, frame):
        # A modal selection menu: the cursor (❯) sits on a numbered option and there
        # are >=2 numbered options. Distinguishes a menu from the input box (where ❯
        # sits on a free-text line, not on "N. ...").
        return bool(_SELECTED_OPTION.search(frame)) and len(_OPTION.findall(frame)) >= 2

    def is_accepting_input(self, frame):
        # The real free-text prompt is present (any permission mode) and it is NOT a menu.
        if self.is_modal_choice(frame):
            return False
        return ("❯" in frame) and (_PROMPT_FOOTER in frame)

    def input_submission_present(self, frame, text):
        # Our submission is in the input box — either the typed text echoes verbatim, or (for a long or
        # multiline task) Claude collapsed it into a "[Pasted text #N]" placeholder on the prompt line.
        needle = " ".join(text.split())[:40]
        if needle and needle in " ".join(frame.split()):
            return True
        # The placeholder only counts on the active input line (from the last ❯ onward), never in
        # scrolled-up agent output that happens to contain the same string.
        tail = frame[frame.rfind("❯"):] if "❯" in frame else ""
        return bool(_PASTED_TEXT.search(tail))
