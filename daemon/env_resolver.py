"""nelix-c5o / nelix-g9k: run a command and use its stdout — the shared, bounded subprocess helper.

A `[executors.X.env_cmd]` entry maps an env var to a shell command; at spawn nelix runs the command
and uses its trimmed stdout as the var's value in the child env. This retires the per-service wrapper
(nelix builds the full launch env and spawns the leaf CLI directly) and lets nelix own the launch env.
nelix-g9k reuses the SAME helper (`run_capture`) for `models_cmd` (read-only model discovery) so
both paths share one `close_fds`/`stdin=DEVNULL`/timeout/bounded-capture/leak discipline.

Fork-safety (spec §4.2): the daemon routes PTY spawns through the single-threaded pty_broker because
`os.forkpty()` runs Python after the fork (deadlock hazard). This is a DIFFERENT mechanism:
`subprocess.Popen` uses `_posixsubprocess`'s C `_fork_exec`, which does an immediate C-level exec with
only async-signal-safe work between fork and exec (no Python post-fork) — the standard, thread-safe
way to run a subprocess. `close_fds=True` (the default) is kept so the child never inherits the PTY
master / control-socket FDs. So this runs on the RPC handler thread, NOT through the broker.

Secret-leak guard (spec §5): `run_capture` is TOTAL — it returns `(value, reason)` and RAISES
NOTHING, so no subprocess exception (a `CalledProcessError` / `TimeoutExpired` traceback embeds the
`['/bin/sh','-c',<command>]` argv, which exc_info logging would then write out) can cross the
boundary. The command / stdout / stderr are never stored or returned. Callers turn a non-None reason
into their OWN typed error, raised OUTSIDE any except so `__context__` stays clean.
"""
import os
import signal
import subprocess
import threading

# Bounded-capture cap for env_cmd stdout: a runtime env value (auth token, backend addr) is tiny, so
# 1 MiB is a generous anti-runaway ceiling, not a tuning knob. models_cmd passes its own cap.
_ENV_CMD_MAX_BYTES = 1 << 20
_READ_CHUNK = 65536
# Upper bound on how long teardown itself may take (reader.join / final reap) AFTER the child's
# process group has been SIGKILLed. Killing the group frees any grandchild-held pipe, so the reader
# hits EOF and the join returns near-instantly; this is only the backstop if that ever stalls.
_CLEANUP_GRACE = 2.0


class EnvResolveError(Exception):
    """A `[executors.X.env_cmd]` command failed to produce a usable value. Carries ONLY `var` +
    `reason` (∈ {non_zero_exit, timeout, empty_output, spawn_failed, decode_failed, output_too_large})
    — never the command, stdout, or stderr — so no sink can leak the secret via this exception."""

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
    # leader, so this also kills a grandchild the command backgrounded (`cmd &`) that inherited the
    # stdout pipe. A plain `proc.kill()` would leave that grandchild holding the write end, so the
    # reader stays blocked in read1() waiting for an EOF that never comes — and even closing our read
    # end then blocks on the BufferedReader lock read1 holds (verified). Killing the group frees the
    # pipe, giving the reader a real EOF. Best-effort: an already-gone group raises OSError (ESRCH).
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


def _reap(proc):
    try:
        proc.wait(timeout=_CLEANUP_GRACE)
    except Exception:
        pass                                   # bounded; a stuck reap must not wedge the call


def _cleanup(proc, reader):
    # Unconditional, BOUNDED teardown run on EVERY exit path so the configured timeout always bounds
    # the call. Kill the group FIRST (frees a grandchild-held pipe -> reader gets EOF), then a bounded
    # join, then close our read end and reap the shell.
    _kill_group(proc)
    if reader is not None:
        try:
            reader.join(_CLEANUP_GRACE)
        except RuntimeError:
            pass                               # thread never started (Thread.start failed) -> nothing to join
    _close(proc.stdout)
    _reap(proc)


def run_capture(command, base_env, timeout, max_bytes):
    """Run `/bin/sh -c command` and capture up to `max_bytes` of stdout, BOUNDED. TOTAL: returns
    `(value, reason)` and RAISES NOTHING — no subprocess exception can drag the command / argv /
    stdout / stderr into a traceback (spec §4.2, §5).

    Success -> `(stdout.rstrip("\\n"), None)` (mirrors shell `$(...)`). Every expected failure maps
    to a redacted reason, never re-raised:
      - non-zero exit           -> `(None, "non_zero_exit")`
      - timeout (child killed)  -> `(None, "timeout")`
      - empty post-strip stdout -> `(None, "empty_output")`
      - OSError / spawn failure -> `(None, "spawn_failed")`
      - non-UTF-8 stdout        -> `(None, "decode_failed")`
      - stdout past `max_bytes` -> `(None, "output_too_large")` (child killed; memory stays bounded)

    Bounded capture (NOT `subprocess.run(capture_output=True)`, which buffers the WHOLE output in
    memory until the child exits — a fast producer would balloon memory within the timeout window):
    a background reader drains stdout so a large producer can't wedge the child on a full pipe, reads
    at most `max_bytes + 1` bytes total, and kills the child the moment it exceeds the cap.

    The configured `timeout` ALWAYS bounds the call. The child runs in its OWN process group
    (`start_new_session`); teardown (`_cleanup`, in a `finally` on every path) SIGKILLs that whole
    group, so a command that exits fast but backgrounds a long-lived child (`cmd &`) inheriting the
    stdout pipe cannot keep the reader blocked in read1() for the child's lifetime — the group-kill
    frees the pipe (EOF), then a BOUNDED `reader.join` / reap finishes teardown.

    stderr -> DEVNULL (never captured — it is redacted anyway — and a full stderr pipe can't deadlock
    the child). stdin=DEVNULL + close_fds default: never inherit the daemon's stdin / PTY / socket FDs.
    """
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdin=subprocess.DEVNULL,          # command reading stdin sees EOF, never the daemon's fd 0
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,         # redacted + a full stderr pipe can't deadlock the child
            env=base_env,                      # close_fds=True is the default: no PTY/socket FD leak
            start_new_session=True,            # own process group -> cleanup can kill backgrounded grandchildren
        )
    except OSError:
        return (None, "spawn_failed")

    chunks = []
    total = 0
    exceeded = False

    def _drain():
        # Read AT MOST max_bytes + 1 bytes; one byte over the cap is enough to prove the producer
        # exceeded it. read1() returns the bytes already available in a single underlying read (it
        # does NOT block for a full `n` like read()), so a slow producer still drains promptly.
        nonlocal total, exceeded
        try:
            while total <= max_bytes:
                b = proc.stdout.read1(min(_READ_CHUNK, max_bytes + 1 - total))
                if not b:
                    return                     # EOF: the child closed stdout
                chunks.append(b)
                total += len(b)
            exceeded = True
            proc.kill()                        # over the cap: kill so proc.wait() returns at once
        except (OSError, ValueError):
            pass                               # pipe closed under us (timeout/overflow kill) -> stop

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        # Bounded teardown on EVERY path (success / timeout / overflow): kill the group so a
        # backgrounded grandchild holding the pipe can't wedge reader.join for its lifetime.
        _cleanup(proc, reader)
    # Result derived AFTER cleanup: the reader has been joined (or bounded-abandoned), so `chunks`
    # and `exceeded` are final and stable.
    if timed_out:
        return (None, "timeout")
    if exceeded:
        return (None, "output_too_large")
    if proc.returncode != 0:
        return (None, "non_zero_exit")
    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        return (None, "decode_failed")
    value = text.rstrip("\n")
    if value == "":
        return (None, "empty_output")
    return (value, None)


def resolve_env_cmds(env_cmd, base_env, timeout):
    """Run each `{var: command}` and return `{var: value}` where value = the command's stdout with
    trailing newlines stripped (mirroring shell `$(...)`). Each command runs via `/bin/sh -c` with
    `env=base_env` (the daemon's ambient env) so whatever the command needs is available. Any failure
    (non-zero exit, timeout, empty/oversized/undecodable stdout, spawn failure) raises
    EnvResolveError(var, reason). An empty `env_cmd` is a no-op ({})."""
    resolved = {}
    for var, command in env_cmd.items():
        value, reason = run_capture(command, base_env, timeout, _ENV_CMD_MAX_BYTES)
        # Raise OUTSIDE any except (there is none — run_capture returned a tuple, no exception is in
        # flight) so EnvResolveError.__context__ / __cause__ are genuinely None (not merely
        # display-suppressed). run_capture already stripped every command / argv / stdout / stderr,
        # so the ONLY thing crossing this boundary is (var, reason) — a structural leak guard (§5).
        if reason is not None:
            raise EnvResolveError(var, reason)
        resolved[var] = value
    return resolved
