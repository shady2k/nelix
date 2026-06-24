import shlex
from pathlib import Path

_WAITER = Path(__file__).parent / "bin" / "nelix-wait"


def arm_waiter(ctx, base, after_seq, token_file):
    """Arm a background wake: a terminal command long-polls /wait and exits on the
    next event, waking Hermes via notify_on_complete.

    The RPC token is read by the waiter from the supervisor state file
    (``token_file``), NOT passed from here: the terminal tool does not forward an
    ``env`` dict, and keeping the token out of argv/the command avoids ps and
    redact_secrets exposure (the token-file path itself is not secret)."""
    cmd = (f"{shlex.quote(str(_WAITER))} --base {shlex.quote(base)}"
           f" --after {int(after_seq)} --token-file {shlex.quote(str(token_file))}")
    return ctx.dispatch_tool("terminal", {
        "command": cmd,
        "background": True,
        "notify_on_complete": True,
    })
