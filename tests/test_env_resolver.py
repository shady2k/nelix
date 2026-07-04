"""nelix-c5o: env_resolver — run a command, use its trimmed stdout as an env value.

These drive the REAL subprocess path (/bin/sh -c) — no mocking of subprocess — so the trim,
pipe, ambient-env, and fail-closed semantics are exercised exactly as they run at spawn.
"""
import os
import traceback

import pytest

from daemon.env_resolver import EnvResolveError, resolve_env_cmds


def test_empty_env_cmd_is_noop():
    assert resolve_env_cmds({}, {}, 5.0) == {}


def test_stdout_becomes_value_and_trailing_newline_stripped():
    # echo appends a newline; the resolved value mirrors shell $(...) and drops it.
    assert resolve_env_cmds({"TOK": "echo hi"}, {}, 5.0) == {"TOK": "hi"}


def test_interior_and_leading_whitespace_preserved():
    # rstrip("\n") strips ONLY trailing newlines, not interior/leading content or trailing spaces.
    out = resolve_env_cmds({"V": "printf 'a b\\n\\n'"}, {}, 5.0)
    assert out == {"V": "a b"}


def test_pipe_and_substitution_work():
    out = resolve_env_cmds({"V": "echo abc | tr a-z A-Z"}, {}, 5.0)
    assert out == {"V": "ABC"}


def test_multiple_vars_resolve_independently():
    out = resolve_env_cmds({"A": "echo 1", "B": "echo 2"}, {}, 5.0)
    assert out == {"A": "1", "B": "2"}


def test_ambient_base_env_visible_to_command():
    # Whatever the command needs (a backend addr, a login token, PATH) comes from base_env.
    out = resolve_env_cmds({"V": "printf %s \"$SEED\""}, {"SEED": "from-ambient", "PATH": "/usr/bin:/bin"}, 5.0)
    assert out == {"V": "from-ambient"}


# ---- fail-closed --------------------------------------------------------------------------
def test_non_zero_exit_raises_env_resolve_error():
    with pytest.raises(EnvResolveError) as ei:
        resolve_env_cmds({"TOK": "exit 3"}, {}, 5.0)
    assert ei.value.var == "TOK"
    assert ei.value.reason == "non_zero_exit"


def test_empty_stdout_raises_env_resolve_error():
    with pytest.raises(EnvResolveError) as ei:
        resolve_env_cmds({"TOK": "true"}, {}, 5.0)     # exit 0 but no stdout
    assert ei.value.reason == "empty_output"


def test_whitespace_only_stdout_is_empty_output():
    with pytest.raises(EnvResolveError) as ei:
        resolve_env_cmds({"TOK": "printf '\\n\\n'"}, {}, 5.0)   # only newlines -> empty post-strip
    assert ei.value.reason == "empty_output"


def test_timeout_raises_and_kills_child():
    with pytest.raises(EnvResolveError) as ei:
        resolve_env_cmds({"TOK": "sleep 5"}, {}, 0.2)
    assert ei.value.var == "TOK"
    assert ei.value.reason == "timeout"


def test_command_reading_stdin_does_not_inherit_daemon_stdin():
    # The resolver passes stdin=subprocess.DEVNULL (like reaper.py): a command that reads stdin sees
    # immediate EOF instead of blocking on / consuming the daemon's stdin. We point the PARENT's fd 0
    # at a pipe that never gets data and never hits EOF (the write end is held open) — without the
    # DEVNULL guard the child would inherit it and `read` would block until the 3s timeout fires.
    r_fd, w_fd = os.pipe()                  # no data ever written; write end kept open -> no EOF
    saved0 = os.dup(0)
    try:
        os.dup2(r_fd, 0)
        out = resolve_env_cmds({"V": "read x; echo ok"}, {}, 3.0)   # must NOT time out
        assert out == {"V": "ok"}
    finally:
        os.dup2(saved0, 0)
        for fd in (saved0, r_fd, w_fd):
            os.close(fd)


# ---- no-leak structure --------------------------------------------------------------------
def test_error_stores_no_command_stdout_or_stderr():
    with pytest.raises(EnvResolveError) as ei:
        resolve_env_cmds({"TOK": "echo LEAKMARKER_STDOUT; echo LEAKMARKER_STDERR 1>&2; exit 1"}, {}, 5.0)
    e = ei.value
    # The exception carries ONLY var + reason — never the command, stdout, or stderr.
    assert e.var == "TOK" and e.reason == "non_zero_exit"
    assert not hasattr(e, "command") and not hasattr(e, "cmd")
    assert not hasattr(e, "stdout") and not hasattr(e, "stderr")
    assert "LEAKMARKER" not in str(e)


def test_error_raised_from_none_breaks_the_traceback_chain():
    # The command lives in a dict (as it does in a real ExecutorSpec), NOT inlined at the call site,
    # so a leak could only come from the CHAINED CalledProcessError's ['/bin/sh','-c',<command>] argv.
    # __context__ is genuinely None (raise happens outside the handler), so a full traceback render
    # (what exc_info=True logging does) can embed neither that argv nor the child's stderr.
    env_cmd = {"TOK": "echo LEAKMARKER_OUT; echo LEAKMARKER_ERR 1>&2; exit 7"}
    try:
        resolve_env_cmds(env_cmd, {}, 5.0)
        raise AssertionError("expected EnvResolveError")
    except EnvResolveError as e:
        assert e.__cause__ is None
        assert e.__context__ is None                    # structural, not just display-suppressed
        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    assert "LEAKMARKER" not in tb
    assert "/bin/sh" not in tb


def test_error_is_not_a_value_error():
    # EnvResolveError must be distinguishable from a client ValueError so /start maps it to 502,
    # not the generic (RuntimeError, ValueError) -> 409.
    assert not issubclass(EnvResolveError, (ValueError, RuntimeError))


# ---- nelix-g9k: run_capture (bounded, TOTAL subprocess helper) ---------------------------
# resolve_env_cmds (above) is now rewritten on top of run_capture; these drive the helper
# directly (real /bin/sh, no mocking except the OSError case) and assert it RAISES NOTHING —
# every outcome is a (value, reason) tuple.
import subprocess
import time

from daemon.env_resolver import run_capture

_CAP = 65536


def test_run_capture_portable_without_os_waitid(monkeypatch):
    # nelix-cb0 anti-recurrence guard: run_capture MUST NOT depend on os.waitid (absent on macOS
    # Python < 3.13 — the daemon runs 3.11). Remove the attribute and prove a trivial command still
    # succeeds; the pre-fix code hit AttributeError -> swallowed -> run_failed for EVERY command.
    monkeypatch.delattr(os, "waitid", raising=False)
    assert run_capture("echo hi", {}, 5.0, _CAP) == ("hi", None)


def test_run_capture_success_strips_trailing_newline():
    assert run_capture("echo hi", {}, 5.0, _CAP) == ("hi", None)


def test_run_capture_interior_whitespace_preserved():
    assert run_capture(r"printf 'a b\n\n'", {}, 5.0, _CAP) == ("a b", None)


def test_run_capture_empty_output():
    assert run_capture("true", {}, 5.0, _CAP) == (None, "empty_output")


def test_run_capture_whitespace_only_is_empty_output():
    assert run_capture(r"printf '\n\n'", {}, 5.0, _CAP) == (None, "empty_output")


def test_run_capture_non_zero_exit():
    # stdout present but a non-zero exit -> non_zero_exit (the value is discarded, never returned).
    assert run_capture("echo out; exit 3", {}, 5.0, _CAP) == (None, "non_zero_exit")


def test_run_capture_timeout_kills_child_and_returns_promptly():
    t0 = time.monotonic()
    assert run_capture("sleep 5", {}, 0.2, _CAP) == (None, "timeout")
    assert time.monotonic() - t0 < 4.0        # returned on the 0.2s deadline, child killed (not 5s)


def test_run_capture_spawn_failed_on_oserror(monkeypatch):
    import daemon.env_resolver as er

    def boom(*a, **k):
        raise OSError("cannot exec")
    monkeypatch.setattr(er.subprocess, "Popen", boom)
    assert run_capture("echo hi", {}, 5.0, _CAP) == (None, "spawn_failed")


def test_run_capture_decode_failed_on_non_utf8_stdout():
    # printf octal escapes emit raw bytes; 0xFF is never a valid UTF-8 byte -> decode_failed.
    assert run_capture(r"printf '\377\376'", {}, 5.0, _CAP) == (None, "decode_failed")


def test_run_capture_output_too_large_bounds_memory_on_file_read(tmp_path):
    # A FINITE producer that writes far past the cap and then EXITS must map to output_too_large, and
    # memory must stay bounded: we read at most max_bytes+1 bytes back from the temp file regardless of
    # how large the file grew (nelix-cb0 removed the infinite-producer-must-be-killed case — an
    # infinite producer now hits the timeout, which is a distinct test; capture is a file, not a pipe).
    big = tmp_path / "big"                          # 100_000 'a' bytes on disk, 1_024-byte cap
    big.write_text("a" * 100_000)
    value, reason = run_capture(f"cat {big}", {}, 10.0, 1024)
    assert (value, reason) == (None, "output_too_large")


def test_run_capture_exactly_at_cap_is_accepted():
    # output length == max_bytes is at the boundary (not OVER it) -> success, not output_too_large.
    assert run_capture("printf 'abcde'", {}, 5.0, 5) == ("abcde", None)


def test_run_capture_one_byte_over_cap_is_too_large():
    assert run_capture("printf 'abcdef'", {}, 5.0, 5) == (None, "output_too_large")


def test_run_capture_ambient_env_visible_to_command():
    out = run_capture('printf %s "$SEED"', {"SEED": "amb", "PATH": "/usr/bin:/bin"}, 5.0, _CAP)
    assert out == ("amb", None)


def test_run_capture_stdin_is_devnull_not_inherited():
    # stdin=DEVNULL: a command that reads stdin sees immediate EOF, never blocks on the daemon's fd 0.
    r_fd, w_fd = os.pipe()                     # never written, write end held open -> no EOF if inherited
    saved0 = os.dup(0)
    try:
        os.dup2(r_fd, 0)
        assert run_capture("read x; echo ok", {}, 3.0, _CAP) == ("ok", None)   # must NOT time out
    finally:
        os.dup2(saved0, 0)
        for fd in (saved0, r_fd, w_fd):
            os.close(fd)


# ---- the timeout ALWAYS bounds the call; a backgrounded child does not block it (nelix-cb0) ----
def test_run_capture_backgrounded_child_does_not_block_the_call():
    # nelix-cb0 accepted trade-off: stdout is a temp FILE, not a pipe, so a command whose /bin/sh
    # exits immediately but leaves a long-lived BACKGROUND child (`cmd &`) does NOT block the call for
    # that child's lifetime — proc.wait() waits only on the shell, which exited. The grandchild is
    # left running (no group-kill except on timeout); these are trusted operator commands. Returns
    # fast with empty_output (the shell produced no stdout).
    t0 = time.monotonic()
    value, reason = run_capture("sleep 30 &", {}, 5.0, _CAP)
    assert (value, reason) == (None, "empty_output")
    assert time.monotonic() - t0 < 5.0        # bounded by the shell's exit, NOT the 30s child lifetime


def test_run_capture_captures_output_before_a_backgrounded_child():
    # Same shape but the command prints first: the output written to the temp file is captured and the
    # call returns fast — the lingering background child does not delay proc.wait() (only the shell is
    # waited on) and does not corrupt the already-written stdout.
    t0 = time.monotonic()
    out = run_capture("echo hi; sleep 30 &", {}, 5.0, _CAP)
    assert out == ("hi", None)
    assert time.monotonic() - t0 < 5.0


# ---- run_capture is TOTAL — no exception escapes after Popen -------------------------------
def test_run_capture_total_on_proc_wait_failure(monkeypatch):
    # An UNEXPECTED proc.wait() failure (any non-TimeoutExpired exception) is caught -> run_failed,
    # not an escaping exception (which could hit the /models generic 500 embedding the argv, or be
    # misclassified by the route's broad `except ValueError` as a wrong 404).
    def boom(self, timeout=None):
        raise RuntimeError("wait blew up")
    monkeypatch.setattr(subprocess.Popen, "wait", boom)
    value, reason = run_capture("echo hi", {}, 5.0, _CAP)
    assert value is None and reason == "run_failed"


def test_run_capture_total_on_non_oserror_popen_failure(monkeypatch):
    # A NON-OSError failure AT Popen (e.g. a bad arg -> ValueError) must also be caught, not escape
    # and get misclassified by the /models route's broad `except ValueError` as a 404.
    import daemon.env_resolver as er

    def boom(*a, **k):
        raise ValueError("bad Popen arg")
    monkeypatch.setattr(er.subprocess, "Popen", boom)
    assert run_capture("echo hi", {}, 5.0, _CAP) == (None, "spawn_failed")


# ---- nelix-cb0: the run_failed path is LOGGED (sanitized), so it is no longer undebuggable ----
class _RecordingLogger:
    """Minimal stand-in for daemon.obs.Logger — records warning() calls as (component, event, fields)."""

    def __init__(self):
        self.records = []

    def warning(self, component, event, session_id=None, **fields):
        self.records.append((component, event, fields))


def test_run_capture_run_failed_logs_sanitized_exc_without_leaking_command(monkeypatch):
    # Force an unexpected proc.wait() failure; run_capture must (a) return (None, "run_failed") and
    # (b) LOG a sanitized record carrying the exception TYPE — with NO command / stdout / stderr / argv
    # anywhere in the log, so the previously-undebuggable failure is visible but leak-free.
    def boom(self, timeout=None):
        raise RuntimeError("wait blew up")
    monkeypatch.setattr(subprocess.Popen, "wait", boom)
    logger = _RecordingLogger()
    value, reason = run_capture("echo SECRET_LEAK_MARKER", {}, 5.0, _CAP, logger=logger)
    assert (value, reason) == (None, "run_failed")
    assert len(logger.records) == 1
    component, event, fields = logger.records[0]
    assert event == "run_capture_failed"
    assert fields["exc_type"] == "RuntimeError"          # the sanitized type IS recorded
    # No command / stdout / argv leaks into the log — not via any field, not via the exc message.
    blob = repr((component, event, fields))
    assert "SECRET_LEAK_MARKER" not in blob
    assert "/bin/sh" not in blob


def test_run_capture_run_failed_without_logger_does_not_raise(monkeypatch):
    # The logger is optional (callers without one still get the total contract): a run_failed with
    # logger=None must not raise.
    def boom(self, timeout=None):
        raise RuntimeError("wait blew up")
    monkeypatch.setattr(subprocess.Popen, "wait", boom)
    assert run_capture("echo hi", {}, 5.0, _CAP) == (None, "run_failed")


def test_run_capture_timeout_does_not_log_run_failed():
    # A timeout is an EXPECTED outcome (its own reason), NOT a run_failed — it must not emit the
    # run_capture_failed record (whose only risk, a TimeoutExpired str embedding the argv, is thereby
    # avoided entirely on this path).
    logger = _RecordingLogger()
    assert run_capture("sleep 5", {}, 0.2, _CAP, logger=logger) == (None, "timeout")
    assert logger.records == []
