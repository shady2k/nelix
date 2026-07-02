import re

from daemon.drivers import register
from daemon.observation import Observation, ObservationCtx, Option, Heartbeat
from daemon.fingerprints import semantic_fp, region_fp

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

# Volatile regions to zero out before measuring semantic stability:
#   braille spinners, "<n.n>s", "<n> tokens", standalone changing counters.
_SPINNER = re.compile(r"[⠀-⣿]")
_ELAPSED = re.compile(r"\d+(?:\.\d+)?s\b")
# Token counters render with a k/M suffix and a decimal once they pass 1000 (e.g. "↓ 33.8k tokens",
# "1.2M tokens"); a bare "\d+ tokens" misses those, leaking the live counter into the fingerprint
# and defeating the engine's anti-flap. Match the optional decimal + magnitude suffix too.
_TOKENS = re.compile(r"\d[\d.,]*[kKmM]?\s+tokens?\b")

# Additional volatile patterns for Claude's transient in-progress tool-status chrome.
# These lines flash and are replaced; they are not conversation content.
_ELLIPSIS_TAIL = re.compile(r"(?:…|\.\.\.)\s*$")   # in-progress status ends in ellipsis
_BARE_TURN_MARKER = re.compile(r"^\s*⏺\s*$")        # lone turn marker, no content
_BACKGROUND_HINT = "(ctrl+b to run in background)"  # background-run hint line

_PROMPT_FOOTER = "shift+tab to cycle"     # present at the interactive input prompt (any mode)
_INPUT_LINE = re.compile(r"^\s*❯")                         # the prompt/input line (anchored)
_RULE_ROW = re.compile(r"^[\s─-╿▀-▟=_]+$")   # box-drawing / rule chars only
_OPTION = re.compile(r"^\s*[❯>]?\s*\d+\.\s+\S", re.M)        # a numbered menu option line
_OPTION_PARSE = re.compile(r"^\s*[❯>]?\s*(\d+)\.\s+(.*\S)\s*$", re.M)  # id + label capture
_SELECTED_OPTION = re.compile(r"^\s*❯\s*\d+\.\s+\S", re.M)   # the cursor sits ON an option
# Claude collapses long/multiline input into "[Pasted text #N]" ON the prompt line; the prompt marker
# (❯) sits immediately before it, and real Claude Code (v2.1.x) renders a NBSP (U+00A0) between them
# — verified from a live capture (s-b8a30317). The class must include the NBSP *explicitly* ( );
# a literal " " here is a plain space (0x20) and silently misses the real placeholder.
_PASTED_TEXT = re.compile("\u276f[ \t\xa0]*" + r"\[Pasted text #\d+\]")

# busy_reason chrome markers (on-screen tool panels only — NEVER the agent's NL output).
_BASH_PANEL = re.compile(r"(?m)^\s*⏺?\s*Bash\(")           # a tool-run panel header
_SUBAGENT_PANEL = re.compile(r"(?m)^\s*⏺?\s*Task\(")       # a sub-agent panel header
# The main-loop status line while ≥1 background subagent runs: "✻ Waiting for N background agent(s)
# to finish". Anchored to a leading spinner glyph (same class as _WORKING_STATUS) so it matches the
# CHROME status line, never the agent's own NL narration — injection-safety, like the panels above.
# Unlike _SUBAGENT_PANEL ("Task(") it also covers custom-typed subagents (e.g. "golang-pro(...)"),
# whose panel header is the agent name, not "Task(".
_BG_AGENT_STATUS = re.compile(r"(?im)^\s*[·✢✳✶✻✽✺✦]\s*waiting for \d+ background agents?")


def _is_choice_prompt(frame):
    # When Claude needs a tool-permission decision it presents a numbered Yes/…/No menu; the
    # Yes+No options are the stable signal that this menu is a permission gate (vs the agent's
    # own "ask the user" numbered menu, which is a plain modal_choice).
    return "1. Yes" in frame and "3. No" in frame


@register("claude")
class ClaudeDriver:
    hook_capable = True         # Claude reports its lifecycle via nelix hooks (--settings injection)
    command_prefixes = ("/",)   # a leading '/' opens a TUI slash-command, not a prompt
    submit_key = "\r"           # the TUI treats CR (not LF) as Enter

    def normalize_frame(self, frame):
        f = _SPINNER.sub("", frame)
        f = _ELAPSED.sub("Ns", f)
        f = _TOKENS.sub("N tokens", f)
        return f

    # ---- actuation: the driver owns the KEYS; Session encodes + writes them to the PTY ----
    def format_submission(self, text):
        # Frame the task as a bracketed paste (ESC[200~ … ESC[201~). Claude enables bracketed-paste
        # mode at startup (ESC[?2004h) and, inside the markers, collapses the input to a single
        # "[Pasted text #N]" placeholder with almost no re-render (0.0s vs 2.2s raw echo for 61.5KB).
        # Markers wrap ONLY the text: the submit key (CR) is pressed separately by the session, so a
        # CR can't be swallowed as paste content.
        return f"\x1b[200~{text}\x1b[201~"

    def submit_text(self, text):
        # A free-text answer: type it raw (the session presses the submit key separately).
        return text

    def select_option(self, id):
        # Pick a numbered modal/permission option: press the digit, then confirm with the submit key.
        return f"{id}{self.submit_key}"

    def interrupt(self):
        # The interrupt key (ESC). The passive daemon never sends it; exposed for completeness.
        return "\x1b"

    # ---- observation: the SOLE classification contract ----
    def observe(self, frame, ctx):
        norm = self.normalize_frame(frame)
        common = dict(
            semantic_fp=semantic_fp(norm),
            content_fp=self._content_fp(norm),
            prompt_fp=self._prompt_fp(norm),
            submitted_echo_present=self._echo_present(frame, ctx.last_submitted_text),
        )

        # 1. terminal, derived from the child (not the screen): crash/exit prompt_kind.
        if not ctx.child_alive:
            kind = "exit" if (ctx.exit_code or 0) == 0 else "crash"
            return Observation(prompt_kind=kind, **common)
        # 2. crash banner on screen while the leader may still be alive.
        if any(m in frame for m in CRASH_MARKERS):
            return Observation(prompt_kind="crash", **common)
        # 3. working / busy — the ❯ box stays visible while the agent works, so "stable + ❯" alone is
        #    NOT idle. A working frame exposes no prompt (prompt_kind=none) but a live heartbeat.
        working_line = self._working_line(frame)
        if working_line is not None:
            aff = set()
            if any(m in frame for m in WORKING_MARKERS):
                aff.add("interrupt_available")
            if _BACKGROUND_HINT in frame:
                aff.add("background_available")
            return Observation(prompt_kind="none", affordances=frozenset(aff),
                               heartbeat=Heartbeat(fp=semantic_fp(working_line), present=True,
                                                   expected_to_change=True),
                               busy_reason=self._busy_reason(frame), **common)
        # 4. modal pick-one menu (cursor on a numbered option, >=2 options). A Yes/No menu is a
        #    permission gate (permission_choice); any other numbered menu is the agent's own
        #    "ask the user" UI (modal_choice). Both are surfaced as a choice with options (fixes F2).
        if self._modal_menu(frame):
            kind = "permission_choice" if _is_choice_prompt(frame) else "modal_choice"
            return Observation(prompt_kind=kind, affordances=frozenset({kind}),
                               options=self._parse_options(frame), **common)
        # 5. the real free-text input box (any permission mode), not a menu. Require the prompt
        #    FOOTER as well as the ❯ marker: a stray ❯ in scrolled output/chrome is NOT an input box,
        #    and delivery must never type into it (the old is_accepting_input safety requirement).
        if any(m in frame for m in INPUT_BOX_MARKERS) and _PROMPT_FOOTER in frame:
            # ...BUT a running background subagent keeps the box live while the main turn is BLOCKED
            # on it (Claude shows "✻ Waiting for N background agent(s) to finish", with the subagent's
            # live token ticker BELOW the ❯ row). That box is not a genuine prompt: read it as busy so
            # the orchestrator is not woken and the ticker can't flap a decision (real-capture
            # s-039a61b4). A real menu (branch 4) wins first, so a permission prompt is never masked.
            bg_line = self._bg_agent_line(frame)
            if bg_line is not None:
                return Observation(prompt_kind="none", busy_reason="waiting_subagents",
                                   heartbeat=Heartbeat(fp=semantic_fp(bg_line), present=True,
                                                       expected_to_change=True), **common)
            return Observation(prompt_kind="free_text",
                               affordances=frozenset({"accepts_text_input"}), **common)
        # 6. a ❯ without the footer (ambiguous chrome) or alive-no-markers -> NOT a wakeable prompt.
        #    `unknown` so the engine treats it as busy and delivery never types into it.
        if any(m in frame for m in INPUT_BOX_MARKERS):
            return Observation(prompt_kind="unknown", busy_reason=self._busy_reason(frame), **common)
        return Observation(prompt_kind="none", busy_reason=self._busy_reason(frame), **common)

    def is_transcript_volatile(self, row):
        # Terminal chrome a human sees but is not conversation content. Anchored patterns only —
        # a loose substring (e.g. any row containing "tokens") would drop real content.
        if _WORKING_STATUS.search(row):                    # spinner status line (+ optional telemetry)
            return True
        if _BG_AGENT_STATUS.search(row):                   # "✻ Waiting for N background agent(s)…"
            return True
        if any(m in row for m in WORKING_MARKERS):         # "esc to interrupt"
            return True
        if _PROMPT_FOOTER in row:                          # "shift+tab to cycle"
            return True
        if _INPUT_LINE.search(row):                        # ❯ prompt / [Pasted text #N] line
            return True
        if row.strip() and _RULE_ROW.match(row):           # pure separator / box rule
            return True
        # Claude's transient in-progress tool-status chrome (lines that flash and are replaced):
        if _ELLIPSIS_TAIL.search(row):                     # in-progress status ends in "…" or "..."
            return True
        if _BARE_TURN_MARKER.match(row):                   # lone ⏺ turn marker with no content
            return True
        if _BACKGROUND_HINT in row:                        # "(ctrl+b to run in background)" hint
            return True
        return False

    # ---- private observation helpers (folded from the old predicates) ----
    def _working_line(self, frame):
        for line in frame.split("\n"):
            if _WORKING_STATUS.search(line) or any(m in line for m in WORKING_MARKERS):
                return line
        return None

    def _modal_menu(self, frame):
        # A modal selection menu: the cursor (❯) sits on a numbered option and there are >=2
        # numbered options. Distinguishes a menu from the input box (where ❯ sits on a free-text
        # line, not on "N. ...").
        return bool(_SELECTED_OPTION.search(frame)) and len(_OPTION.findall(frame)) >= 2

    def _parse_options(self, frame):
        return tuple(Option(m.group(1), m.group(2).strip())
                     for m in _OPTION_PARSE.finditer(frame))

    def _echo_present(self, frame, text):
        # Our submission is in the ACTIVE input region — either the typed text echoes verbatim, or
        # (for a long/multiline task) Claude collapsed it into a "[Pasted text #N]" placeholder on
        # the prompt line. Scoped to the prompt tail (last ❯ onward), never scrollback.
        if not text:
            return False
        # Scope BOTH checks to the prompt TAIL (last ❯ onward) — never scrollback: our text appearing
        # in agent output must not be read as still-in-the-box (spec §5.5/§10, BLOCKER 2).
        tail = frame[frame.rfind("❯"):] if "❯" in frame else ""
        needle = " ".join(text.split())[:40]
        if needle and needle in " ".join(tail.split()):
            return True
        return bool(_PASTED_TEXT.search(tail))

    def _busy_reason(self, frame):
        # On-screen chrome ONLY (tool panels / status line), never the agent's NL output (injection
        # safety). The subagent status line + the "Task(" panel both mean the turn is blocked on a
        # subagent; the status line also covers custom-typed subagents the panel regex would miss.
        if _BASH_PANEL.search(frame):
            return "running_command"
        if _BG_AGENT_STATUS.search(frame) or _SUBAGENT_PANEL.search(frame):
            return "waiting_subagents"
        return None

    def _bg_agent_line(self, frame):
        for line in frame.split("\n"):
            if _BG_AGENT_STATUS.search(line):
                return line
        return None

    def _last_input_row(self, rows):
        last = None
        for i, r in enumerate(rows):
            if "❯" in r:
                last = i
        return last

    def _content_fp(self, norm):
        # "did real executor output change" — hash the frame EXCLUDING the active input row.
        rows = norm.split("\n")
        idx = self._last_input_row(rows)
        if idx is None:
            return region_fp(norm)
        return region_fp(norm, exclude=(idx, len(rows)))

    def _prompt_fp(self, norm):
        # "did the published prompt change / leave" — hash ONLY the prompt/affordance region (the
        # active input row to the end of the frame, which for a modal covers the option lines).
        rows = norm.split("\n")
        idx = self._last_input_row(rows)
        if idx is None:
            return region_fp(norm)
        return region_fp(norm, keep=(idx, len(rows)))

    def modal_body_fp(self, norm):
        # A STABLE fingerprint of a choice modal's QUESTION BLOCK — the contiguous nonblank question
        # rows immediately above the option block, bounded above by a blank, the modal's top rule
        # border, or the bound (<=8 rows). Used by _emit_blocked to dedup a flickering modal across
        # repaints while keeping two same-option different-question modals distinct. The region is
        # ANCHORED to the options (walk up from the option block) and BOUNDED, so it stays put across
        # a repaint AND excludes the volatile streaming scrollback ABOVE the modal: the gopls flicker
        # repaints the streaming diff above the modal (even overwriting the modal's own top border as
        # it pushes down), but the bottom question row(s) right above the options hold still, so the
        # SAME modal collapses while a multi-line question that differs does not false-collapse.
        # Returns None if there is no option row or no question row above it.
        rows = norm.split("\n")
        first_opt = next((i for i, r in enumerate(rows) if _OPTION.search(r)), None)
        if first_opt is None or first_opt == 0:
            return None
        end = first_opt - 1
        while end >= 0 and not rows[end].strip():          # skip spacer blanks directly above options
            end -= 1
        if end < 0:
            return None
        start = end
        while (start > max(-1, first_opt - 9)              # bounded: never reach past modal into scrollback
               and rows[start].strip()                      # contiguous nonblank question rows
               and not _RULE_ROW.match(rows[start])):       # stop at the modal's top rule border
            start -= 1
        # `start` landed on the boundary (blank / rule) or the bound edge; the block starts just after.
        if start < 0 or not rows[start].strip() or _RULE_ROW.match(rows[start]):
            start += 1
        if start > end:
            return None
        return region_fp(norm, keep=(start, end + 1))
