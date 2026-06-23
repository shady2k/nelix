import re

# Control chars except none-kept; ESC sequences (CSI/OSC) removed.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # keep \t? no: strip all control


def sanitize_answer(text: str) -> str:
    """Make an answer safe to inject into a PTY: no escape/control sequences,
    no stray newlines (the Session adds the submit CR itself), and no leading
    slash so it cannot be read as an executor slash-command."""
    s = _ANSI.sub("", text)
    s = s.replace("\r", "").replace("\n", " ")
    s = _CTRL.sub("", s)
    s = " ".join(s.split())          # collapse whitespace, trim ends
    if s.startswith("/"):
        s = s[1:].lstrip()
    return s
