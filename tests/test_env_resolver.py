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
import sys
import threading
import time

from daemon.env_resolver import run_capture

_CAP = 65536


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


def test_run_capture_output_too_large_kills_producer_and_bounds_memory():
    # An UNBOUNDED producer past the cap must be KILLED (return promptly), NOT buffered until the
    # timeout — this is the bounded-capture guarantee subprocess.run(capture_output=True) lacks. A
    # generous 10s timeout: had we returned via timeout instead of the cap-kill, the call would take
    # ~10s; it returns in well under a second, proving the child was killed at the cap.
    t0 = time.monotonic()
    value, reason = run_capture("while :; do printf 'xxxxxxxxxxxxxxxx'; done", {}, 10.0, 1024)
    assert (value, reason) == (None, "output_too_large")
    assert time.monotonic() - t0 < 5.0        # killed on the cap, did NOT run to the 10s timeout


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


# ---- FIX 1: the configured timeout must ALWAYS bound the call -----------------------------
def test_run_capture_backgrounded_child_does_not_bypass_timeout():
    # A command whose /bin/sh exits immediately but leaves a long-lived BACKGROUND child holding the
    # stdout pipe must NOT make the call block for that child's lifetime: the reader thread wedges in
    # read1() waiting for an EOF the surviving grandchild never sends, and merely closing our read end
    # blocks on the BufferedReader lock read1 holds. Cleanup kills the whole process group, freeing
    # the pipe, so the call returns fast with a redacted reason (here empty_output — the shell exited
    # 0 with no output).
    t0 = time.monotonic()
    value, reason = run_capture("sleep 30 &", {}, 0.2, _CAP)
    assert value is None and reason in {"empty_output", "timeout"}
    assert time.monotonic() - t0 < 5.0        # bounded — NOT the 30s child lifetime


def test_run_capture_captures_output_before_a_backgrounded_child():
    # Same shape but the command prints first: the buffered output is still captured (closing a pipe's
    # write end does not discard already-buffered bytes) and the call returns fast — the lingering
    # background child is reaped by the process-group cleanup, not waited on.
    t0 = time.monotonic()
    out = run_capture("echo hi; sleep 30 &", {}, 5.0, _CAP)
    assert out == ("hi", None)
    assert time.monotonic() - t0 < 5.0


# ---- FIX 2: run_capture is TOTAL — no exception escapes after Popen -----------------------
def test_run_capture_total_on_thread_start_failure(monkeypatch):
    # A post-spawn failure (here Thread.start raising) must map to a redacted reason, never escape:
    # an escaping exception could hit the /models generic 500 (embedding the argv in a traceback) or
    # be misclassified by the route's broad `except ValueError` as a wrong 404. Cleanup still reaps
    # the spawned child (bounded), so nothing leaks.
    def boom(self):
        raise RuntimeError("cannot start thread")
    monkeypatch.setattr(threading.Thread, "start", boom)
    value, reason = run_capture("echo hi", {}, 5.0, _CAP)
    assert value is None and reason == "run_failed"


def test_run_capture_total_on_proc_wait_failure(monkeypatch):
    # A proc.wait() failure (any non-TimeoutExpired exception) is also caught -> run_failed, not an
    # escaping exception.
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


# ---- FIX A: the group-kill must run while proc.pid is still OWNED (unreaped) --------------
def test_run_capture_group_kill_happens_before_reap(monkeypatch):
    # PID-reuse safety: teardown must SIGKILL the child's process group BEFORE reaping the child.
    # Once reaped, proc.pid is released and can be reused as an unrelated pgid, so a later
    # killpg(proc.pid) could hit the WRONG group. Assert the order structurally (killpg strictly
    # before the reaping wait) and that the child is NOT reaped when killpg fires.
    import daemon.env_resolver as er
    order = []
    real_killpg = os.killpg
    real_reap = er._reap

    def spy_killpg(pgid, sig):
        assert "reap" not in order, "child was reaped BEFORE the group-kill (pid-reuse race)"
        order.append("killpg")
        return real_killpg(pgid, sig)

    def spy_reap(proc):
        order.append("reap")
        return real_reap(proc)
    monkeypatch.setattr(er.os, "killpg", spy_killpg)
    monkeypatch.setattr(er, "_reap", spy_reap)
    assert run_capture("echo hi", {}, 5.0, _CAP) == ("hi", None)
    assert order == ["killpg", "reap"]            # group-kill strictly before the reaping wait


def test_run_capture_primary_wait_is_non_reaping_wnowait(monkeypatch):
    # The primary wait must observe the child's exit WITHOUT reaping it (os.waitid WNOWAIT), so the
    # pid stays owned until the group-kill. Assert every primary-wait call carries WNOWAIT.
    import daemon.env_resolver as er
    seen = []
    real_waitid = os.waitid

    def spy_waitid(idtype, id, options):
        seen.append(options)
        return real_waitid(idtype, id, options)
    monkeypatch.setattr(er.os, "waitid", spy_waitid)
    assert run_capture("echo hi", {}, 5.0, _CAP) == ("hi", None)
    assert seen, "primary wait must use os.waitid"
    assert all(o & os.WNOWAIT for o in seen), "primary wait must be WNOWAIT (must not reap)"


def test_run_capture_total_on_primary_wait_failure(monkeypatch):
    # The primary wait is now os.waitid; an UNEXPECTED error there must be caught -> run_failed, never
    # an escaping exception (total guarantee preserved after the FIX A restructure).
    import daemon.env_resolver as er

    def boom(*a, **k):
        raise RuntimeError("waitid blew up")
    monkeypatch.setattr(er.os, "waitid", boom)
    value, reason = run_capture("echo hi", {}, 5.0, _CAP)
    assert value is None and reason == "run_failed"


# ---- FIX B/C: a reader that never EOFs is ABANDONED; cleanup never hangs past ~grace -------
def test_run_capture_escaping_grandchild_returns_within_grace(tmp_path):
    # A backgrounded grandchild that setsid()s OUT of the child's process group keeps the stdout
    # write-end open: killpg can't free the pipe, so the reader stays wedged in read1() forever. The
    # call must still RETURN within ~timeout+grace with a redacted reason (never hang, never block in
    # _close on the read1 buffer lock).
    #
    # Deterministic escape: the grandchild touches a sentinel ONLY after setsid(), and the shell waits
    # for it before exiting — so by the time teardown's killpg fires, the grandchild has provably left
    # the group (no race with interpreter startup) and still holds the pipe.
    sentinel = tmp_path / "escaped"
    script = tmp_path / "esc.py"
    script.write_text(
        "import os, time\n"
        "os.setsid()\n"                                 # leave the shell's process group
        f"open({str(sentinel)!r}, 'w').close()\n"       # signal 'I have escaped'
        "time.sleep(5)\n")                              # hold the stdout pipe past timeout+grace
    esc = (f"{sys.executable} {script} & "
           f"while [ ! -f {sentinel} ]; do sleep 0.02; done")
    t0 = time.monotonic()
    value, reason = run_capture(esc, {}, 5.0, _CAP)
    dt = time.monotonic() - t0
    assert value is None and reason == "run_failed"
    assert dt < 8.0, f"cleanup hung ({dt:.2f}s) — must be bounded by ~timeout+grace"


def test_run_capture_wedged_reader_abandons_without_partial_value_or_close(monkeypatch):
    # FIX C: when the reader has NOT confirmed-exited, the shared capture state (chunks/exceeded) is
    # not stable, so run_capture must return a failure reason WITHOUT deriving a value from it — and
    # FIX B: it must NOT close proc.stdout from the caller thread (that would block on the read1 lock).
    # Force reader_clean=False even though `echo hi` really did produce "hi\n": the result must be
    # run_failed, never the partial ("hi", None), and _close must not be invoked.
    import daemon.env_resolver as er
    monkeypatch.setattr(er, "_join_reader", lambda reader, grace: False)
    closed = []
    real_close = er._close
    monkeypatch.setattr(er, "_close", lambda f: closed.append(f))
    assert run_capture("echo hi", {}, 5.0, _CAP) == (None, "run_failed")
    assert closed == [], "proc.stdout must NOT be closed when the reader is abandoned (would block)"
