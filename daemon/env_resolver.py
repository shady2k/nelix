"""nelix-c5o: resolve runtime env values by running a command and using its stdout.

A `[executors.X.env_cmd]` entry maps an env var to a shell command; at spawn nelix runs the command
and uses its trimmed stdout as the var's value in the child env. This retires the per-service wrapper
(nelix builds the full launch env and spawns the leaf CLI directly) and lets nelix own the launch env.

Fork-safety (spec §4.2): the daemon routes PTY spawns through the single-threaded pty_broker because
`os.forkpty()` runs Python after the fork (deadlock hazard). This is a DIFFERENT mechanism:
`subprocess.run` uses `_posixsubprocess`'s C `_fork_exec`, which does an immediate C-level exec with
only async-signal-safe work between fork and exec (no Python post-fork) — the standard, thread-safe
way to run a subprocess. `close_fds=True` (the default) is kept so the child never inherits the PTY
master / control-socket FDs. So this runs on the RPC handler thread, NOT through the broker.

Secret-leak guard (spec §5): on failure we raise EnvResolveError(var, reason) **from None**, storing
NEITHER the command NOR stdout/stderr — a chained CalledProcessError / TimeoutExpired traceback embeds
the ['/bin/sh','-c',<command>] argv, which exc_info logging would then write out (a command can carry
a secret path). The message names only the VAR and a generic reason.
"""
import subprocess


class EnvResolveError(Exception):
    """A `[executors.X.env_cmd]` command failed to produce a usable value: non-zero exit, timeout, or
    empty (post-strip) stdout. Carries ONLY `var` + `reason` (∈ {non_zero_exit, timeout, empty_output})
    — never the command, stdout, or stderr — so no sink can leak the secret via this exception."""

    def __init__(self, var, reason):
        super().__init__(f"env_cmd for {var!r} failed: {reason}")
        self.var = var
        self.reason = reason


def resolve_env_cmds(env_cmd, base_env, timeout):
    """Run each `{var: command}` and return `{var: value}` where value = the command's stdout with
    trailing newlines stripped (mirroring shell `$(...)`). Each command runs via `/bin/sh -c` with
    `env=base_env` (the daemon's ambient env) so whatever the command needs is available. A non-zero
    exit, a timeout (the child is killed), or empty post-strip stdout raises EnvResolveError from None
    (no command/stdout/stderr retained). An empty `env_cmd` is a no-op ({})."""
    resolved = {}
    for var, command in env_cmd.items():
        # Capture the failure reason INSIDE the handler but raise OUTSIDE it: once control leaves the
        # except block Python has cleared the handled exception, so EnvResolveError.__context__ is
        # genuinely None (not merely display-suppressed). Nothing that could carry the command / argv
        # / stderr is ever referenced by the raised exception — a structural leak guard (spec §5).
        reason = None
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", command],
                capture_output=True, text=True, timeout=timeout, env=base_env,
                stdin=subprocess.DEVNULL,         # never inherit/consume the daemon's stdin: a
                                                  # stdin-reading command sees EOF, not a hang
                                                  # (mirrors reaper.py's subprocess discipline)
                check=True,                       # non-zero exit -> CalledProcessError
            )
        except subprocess.TimeoutExpired:
            reason = "timeout"                    # child already killed by subprocess.run
        except subprocess.CalledProcessError:
            reason = "non_zero_exit"
        if reason is not None:
            raise EnvResolveError(var, reason) from None
        value = proc.stdout.rstrip("\n")
        if value == "":
            raise EnvResolveError(var, "empty_output") from None
        resolved[var] = value
    return resolved
