import re
import unicodedata

# ANSI CSI/OSC escape sequences (output-style escapes that can be embedded in input).
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# Bidi overrides/isolates (U+202A..202E, U+2066..2069) — "Trojan-source" reordering controls,
# never legitimate in a typed task/answer. Built via chr() so this source stays pure ASCII.
# ZWJ and other Cf are intentionally kept (emoji / Indic scripts use them).
_BIDI = re.compile("[%s]" % "".join(
    chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))))


class PtyInputRejected(ValueError):
    """Text typed into a PTY that is unsafe even after byte hygiene: it would be read
    as a CLI command, or it sanitizes to nothing. Raise rather than silently rewrite."""


def core_sanitize(text: str) -> str:
    """CLI-AGNOSTIC byte hygiene for any text typed into a PTY. Drops escape/control
    sequences and stray control characters (so embedded bytes can't act as keystrokes —
    Shift+Tab, Enter, Ctrl-C, ...), flattens newlines (the Session adds the single submit
    key itself), and collapses whitespace. Knows nothing about any CLI's command syntax —
    that is the driver's concern (see prepare_pty_input / Driver.command_prefixes)."""
    s = _ANSI.sub("", text)
    s = _BIDI.sub("", s)
    s = s.replace("\r", "")                            # CR == submit keystroke; drop it
    s = re.sub(r"[\t\n\v\f]", " ", s)                  # other whitespace controls -> space (incl TAB)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cc")  # ESC, NUL, DEL, C1, ...
    return " ".join(s.split())                         # collapse runs of whitespace, trim ends


def prepare_pty_input(text: str, command_prefixes=("/",)) -> str:
    """Core byte hygiene plus the driver's semantic policy. Refuses input that sanitizes
    to nothing, or that begins with one of the CLI's command prefixes (so a prompt cannot
    be misread as a command). Rejects rather than silently rewriting: stripping a leading
    '/' would turn '/exit' into 'exit' and '/etc/x' into 'etc/x' invisibly."""
    if not isinstance(text, str):                 # a non-str RPC field would TypeError in re.sub
        raise PtyInputRejected(f"expected a string, got {type(text).__name__}")
    s = core_sanitize(text)
    if not s:
        raise PtyInputRejected("input is empty after sanitization")
    for p in command_prefixes:
        if p and s.startswith(p):
            raise PtyInputRejected(
                f"input starts with command prefix {p!r}; it would be read as a command, "
                f"not a prompt — rephrase without a leading {p!r}")
    return s
