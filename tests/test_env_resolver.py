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
