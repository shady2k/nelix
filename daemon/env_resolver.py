"""nelix-c5o / nelix-g9k: run a command and use its stdout — the shared, bounded subprocess helper.

A `[executors.X.env_cmd]` entry maps an env var to a shell command; at spawn nelix runs the command
and uses its trimmed stdout as the var's value in the child env. This retires the per-service wrapper
(nelix builds the full launch env and spawns the leaf CLI directly) and lets nelix own the launch env.
nelix-g9k reuses the SAME helper (`run_capture`) for `models_cmd` (read-only model discovery) so
both paths share one `close_fds`/`stdin=DEVNULL`/timeout/bounded-capture/leak discipline.

nelix-cb0: capture is via a TEMP FILE, not a pipe + reader thread. The daemon runs Python 3.11
(macOS), where `os.waitid` does NOT exist (added on macOS in 3.13); the previous pipe design used
`os.waitid` to observe the child's exit without reaping it, so on the real daemon EVERY command hit
AttributeError -> the broad `except` -> a spurious `run_failed` (env_cmd + models_cmd both dead
despite green 3.14 tests). Writing stdout to a `tempfile.TemporaryFile` removes the pipe entirely:
`proc.wait(timeout=...)` alone bounds the call, no background reader, no `os.waitid`, no pipe-deadlock
to defend against, and memory stays bounded because we read at most `max_bytes + 1` back from the file.

Fork-safety (spec §4.2): the daemon routes PTY spawns through the single-threaded pty_broker because
`os.forkpty()` runs Python after the fork (deadlock hazard). This is a DIFFERENT mechanism:
`subprocess.Popen` uses `_posixsubprocess`'s C `_fork_exec`, which does an immediate C-level exec with
only async-signal-safe work between fork and exec (no Python post-fork) — the standard, thread-safe
way to run a subprocess. `close_fds=True` (the default) is kept so the child never inherits the PTY
master / control-socket FDs. So this runs on the RPC handler thread, NOT through the broker.

Secret-leak guard (spec §5): `run_capture` is TOTAL — it returns `(value, reason)` and RAISES
NOTHING, so no subprocess exception (a `CalledProcessError` / `TimeoutExpired` traceback embeds the
`['/bin/sh','-c',<command>]` argv, which exc_info logging would then write out) can cross the
boundary. The command / stdout / stderr are never stored or returned. On the `run_failed` path an
optional `logger` records ONLY `type(exc).__name__` (+ `str(exc)` unless it is a TimeoutExpired /
CalledProcessError, whose str embeds the argv) so the previously-undebuggable failure is visible
without leaking the secret. Callers turn a non-None reason into their OWN typed error, raised OUTSIDE
any except so `__context__` stays clean.
"""
import os
import signal
import subprocess
import tempfile

# Bounded-capture cap for env_cmd stdout: a runtime env value (auth token, backend addr) is tiny, so
# 1 MiB is a generous anti-runaway ceiling, not a tuning knob. models_cmd passes its own cap.
_ENV_CMD_MAX_BYTES = 1 << 20
# Upper bound on the best-effort reap AFTER a timeout/error SIGKILL. Killing the process group makes
# the child reapable at once; this is only the backstop if that ever stalls.
_CLEANUP_GRACE = 2.0


class EnvResolveError(Exception):
    """A `[executors.X.env_cmd]` command failed to produce a usable value. Carries ONLY `var` +
    `reason` (∈ {non_zero_exit, timeout, empty_output, spawn_failed, decode_failed, output_too_large,
    run_failed}) — never the command, stdout, or stderr — so no sink can leak the secret via this
    exception."""

    def __init__(self, var, reason):
        super().__init__(f"env_cmd for {var!r} failed: {reason}")
        self.var = var
        self.reason = reason


def _close(f):
    try:
        f.close()
    except (OSError, ValueError):
        pass


def _kill_group(proc):
    # SIGKILL the child's WHOLE process group. `start_new_session=True` makes the shell the group
    # leader, so this also kills any grandchild the command left in the group. Best-effort: an
    # already-gone group raises OSError (ESRCH). Called ONLY on the timeout / unexpected-error paths
    # while the child is still UNREAPED, so `proc.pid` still owns the group (no pid-reuse race).
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


def _reap_quiet(proc):
    # Best-effort bounded reap so a SIGKILLed child does not linger as a zombie. TOTAL: swallows a
    # TimeoutExpired (child somehow not yet reapable) and any other wait failure — teardown must
    # never raise or block past the grace.
    try:
        proc.wait(timeout=_CLEANUP_GRACE)
    except Exception:
        pass


def _log_run_failed(logger, exc):
    """Record a `run_failed` (an UNEXPECTED post-spawn exception) so it is debuggable, without leaking
    the secret. Always logs `type(exc).__name__`; logs `str(exc)` ONLY when `exc` is not a
    `TimeoutExpired` / `CalledProcessError` — the str of those embeds the `['/bin/sh','-c',<command>]`
    argv. (With this design neither is raised on the run_failed path, since the dedicated
    `except TimeoutExpired` handles timeouts and `check=True` is never used; the guard is
    defence-in-depth.) A None logger is a no-op."""
    if logger is None:
        return
    if isinstance(exc, (subprocess.TimeoutExpired, subprocess.CalledProcessError)):
        exc_msg = None
    else:
        exc_msg = str(exc)
    logger.warning("env_resolver", "run_capture_failed",
                   exc_type=type(exc).__name__, exc_msg=exc_msg)


def run_capture(command, base_env, timeout, max_bytes, logger=None):
    """Run `/bin/sh -c command` and capture up to `max_bytes` of stdout, BOUNDED. TOTAL: returns
    `(value, reason)` and RAISES NOTHING — no subprocess exception can drag the command / argv /
    stdout / stderr into a traceback (spec §4.2, §5).

    Success -> `(stdout.rstrip("\\n"), None)` (mirrors shell `$(...)`). Every expected failure maps
    to a redacted reason, never re-raised:
      - non-zero exit           -> `(None, "non_zero_exit")`
      - timeout (child killed)  -> `(None, "timeout")`
      - empty post-strip stdout -> `(None, "empty_output")`
      - spawn failure (any exc) -> `(None, "spawn_failed")`
      - non-UTF-8 stdout        -> `(None, "decode_failed")`
      - stdout past `max_bytes`  -> `(None, "output_too_large")` (memory stays bounded)
      - any other post-spawn failure (proc.wait / read / ...) -> `(None, "run_failed")` (LOGGED)

    Capture is via a `tempfile.TemporaryFile`, NOT a pipe: `proc.wait(timeout)` alone bounds the call
    — no background reader, no pipe-deadlock defence, and (crucially) no `os.waitid`, which does not
    exist on the daemon's Python 3.11 (nelix-cb0). Memory stays bounded because at most `max_bytes+1`
    bytes are read back from the file; an over-cap FINITE producer is detected on that read. The
    accepted trade-off (vs. the old group-kill-on-overflow reader): a command that BACKGROUNDS a child
    (`cmd &`) leaves that grandchild running after the shell exits — these are trusted, operator-
    authored secret/model commands, so that is acceptable. The process group is SIGKILLed only when
    the call itself must be torn down (timeout / unexpected error).

    stderr -> DEVNULL (never captured — it is redacted anyway — and can't fill/deadlock anything).
    stdin=DEVNULL + close_fds default: never inherit the daemon's stdin / PTY / socket FDs. The child
    runs in its OWN process group (`start_new_session`) so a timeout kill reaches backgrounded kin.
    `logger`, when provided, records a sanitized record on the `run_failed` path only (see
    `_log_run_failed`)."""
    try:
        tmp = tempfile.TemporaryFile()          # binary; stdout target. unlinked at once on POSIX.
    except Exception as e:
        # A temp-file setup failure is unexpected and has no secret in it; surface it as run_failed
        # (LOGGED) rather than a spawn we never attempted, and stay TOTAL.
        _log_run_failed(logger, e)
        return (None, "run_failed")
    try:
        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", command],
                stdin=subprocess.DEVNULL,       # command reading stdin sees EOF, never the daemon's fd 0
                stdout=tmp,                     # -> temp file, no pipe (no reader thread, no os.waitid)
                stderr=subprocess.DEVNULL,      # redacted + can't fill a pipe / deadlock the child
                env=base_env,                   # close_fds=True is the default: no PTY/socket FD leak
                start_new_session=True,         # own process group -> a timeout kill reaches grandchildren
            )
        except Exception:
            # OSError (exec failed) or ANY other spawn-time failure (a bad env value -> ValueError, ...):
            # redacted reason. No proc exists to clean up (Popen closes its own fds on failure).
            return (None, "spawn_failed")

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Race-free: the child is still UNREAPED here, so proc.pid still owns the group. Kill the
            # whole group, then best-effort reap within the grace.
            _kill_group(proc)
            _reap_quiet(proc)
            return (None, "timeout")
        except Exception as e:
            # Any OTHER post-spawn failure (e.g. proc.wait itself blowing up). Best-effort: if the
            # child is still running, group-kill it; then reap. Redacted reason + LOG the exc detail.
            try:
                still_running = proc.poll() is None   # poll reaps only an already-EXITED child (safe)
            except Exception:
                still_running = True
            if still_running:
                _kill_group(proc)
            _reap_quiet(proc)
            _log_run_failed(logger, e)
            return (None, "run_failed")

        # proc.wait returned -> the child has exited and is reaped.
        if proc.returncode != 0:
            return (None, "non_zero_exit")
        try:
            tmp.seek(0)
            data = tmp.read(max_bytes + 1)      # read one past the cap: proves an over-cap producer
        except Exception as e:
            _log_run_failed(logger, e)
            return (None, "run_failed")
        if len(data) > max_bytes:
            return (None, "output_too_large")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return (None, "decode_failed")
        value = text.rstrip("\n")
        if value == "":
            return (None, "empty_output")
        return (value, None)
    finally:
        _close(tmp)                             # the temp file is ALWAYS closed on every path


def resolve_env_cmds(env_cmd, base_env, timeout, logger=None):
    """Run each `{var: command}` and return `{var: value}` where value = the command's stdout with
    trailing newlines stripped (mirroring shell `$(...)`). Each command runs via `/bin/sh -c` with
    `env=base_env` (the daemon's ambient env) so whatever the command needs is available. Any failure
    (non-zero exit, timeout, empty/oversized/undecodable stdout, spawn failure) raises
    EnvResolveError(var, reason). An empty `env_cmd` is a no-op ({}). `logger` (optional) is threaded
    to run_capture so a `run_failed` is recorded sanitized (var/reason only cross THIS boundary)."""
    resolved = {}
    for var, command in env_cmd.items():
        value, reason = run_capture(command, base_env, timeout, _ENV_CMD_MAX_BYTES, logger=logger)
        # Raise OUTSIDE any except (there is none — run_capture returned a tuple, no exception is in
        # flight) so EnvResolveError.__context__ / __cause__ are genuinely None (not merely
        # display-suppressed). run_capture already stripped every command / argv / stdout / stderr,
        # so the ONLY thing crossing this boundary is (var, reason) — a structural leak guard (§5).
        if reason is not None:
            raise EnvResolveError(var, reason)
        resolved[var] = value
    return resolved
