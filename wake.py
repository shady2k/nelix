import shlex
from pathlib import Path

_WAITER = Path(__file__).parent / "bin" / "nelix-wait"


def arm_waiter(ctx, after_seq, state_file, session_id):
    """Arm a background wake: a terminal command long-polls /wait and exits on the
    next event, waking Hermes via notify_on_complete. ``session_id`` is REQUIRED — the
    waiter ALWAYS scopes /wait to that session so a cross-session event can never wake it
    (and never builds a session-less nelix-wait command, which the CLI rejects anyway).

    The waiter discovers the RPC endpoint and token from ``state_file`` (the supervisor
    state file written on daemon start). This keeps the token out of argv / ps exposure
    and avoids relying on an ``env`` dict that the terminal tool does not forward."""
    cmd = (f"{shlex.quote(str(_WAITER))} --state-file {shlex.quote(str(state_file))}"
           f" --after {int(after_seq)}"
           f" --session-id {shlex.quote(str(session_id))}")
    return ctx.dispatch_tool("terminal", {
        "command": cmd,
        "background": True,
        "notify_on_complete": True,
    })
