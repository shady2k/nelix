import pytest

from daemon.hygiene import core_sanitize, prepare_pty_input, PtyInputRejected


# ---- core_sanitize: CLI-agnostic byte hygiene ----

def test_core_strips_escape_control_and_flattens_newlines():
    assert core_sanitize("1\x1b[2J\n") == "1"          # CSI erase removed, trailing NL gone
    assert core_sanitize("ye\rs\n\n") == "yes"         # CR dropped (it is submit), NLs collapsed
    assert core_sanitize("a\tb") == "a b"              # TAB neutralized (it used to survive — bug)
    assert core_sanitize("x\x1b[Zy") == "xy"           # bare CSI (Shift+Tab keystroke) removed
    assert core_sanitize("\x07\x00bell") == "bell"     # stray C0 controls dropped


def test_core_strips_bidi_overrides():
    rlo = chr(0x202E)                                  # RIGHT-TO-LEFT OVERRIDE
    assert core_sanitize("ab" + rlo + "cd") == "abcd"  # Trojan-source class removed
    lri, pdi = chr(0x2066), chr(0x2069)                # bidi isolates
    assert core_sanitize("a" + lri + "b" + pdi + "c") == "abc"


def test_core_keeps_plain_and_unicode_text():
    assert core_sanitize("2") == "2"
    assert core_sanitize("привет ❯") == "привет ❯"     # Cyrillic + U+276F content preserved
    assert core_sanitize("use 😀 emoji") == "use 😀 emoji"


# ---- prepare_pty_input: byte hygiene + driver semantic policy ----

def test_prepare_rejects_leading_command_prefix():
    # Reject rather than silently rewrite — stripping '/' would corrupt both '/exit' and '/etc/x'.
    with pytest.raises(PtyInputRejected):
        prepare_pty_input("/exit")
    with pytest.raises(PtyInputRejected):
        prepare_pty_input("  /quit now ")


def test_prepare_rejects_empty_after_sanitize():
    with pytest.raises(PtyInputRejected):
        prepare_pty_input("\x1b[2J\n")                 # nothing left to send


def test_prepare_rejects_non_string():
    # a non-str RPC field (e.g. {"task": null} / {"answer": 1}) must 400, not TypeError -> 500
    for bad in (None, 123, ["x"], {"a": 1}):
        with pytest.raises(PtyInputRejected):
            prepare_pty_input(bad)


def test_prepare_passes_plain_input():
    assert prepare_pty_input("2") == "2"
    assert prepare_pty_input("use the staging db") == "use the staging db"


def test_prepare_respects_driver_prefix_override():
    # A driver whose CLI has no command prefix lets a leading slash (e.g. a path) through.
    assert prepare_pty_input("/path/ok", command_prefixes=()) == "/path/ok"
    # A driver using ':' rejects ':wq' but not '/foo'.
    with pytest.raises(PtyInputRejected):
        prepare_pty_input(":wq", command_prefixes=(":",))
    assert prepare_pty_input("/foo", command_prefixes=(":",)) == "/foo"
