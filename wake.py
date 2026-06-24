import os
import shlex
from pathlib import Path

_WAITER = Path(__file__).parent / "bin" / "nelix-wait"


def arm_waiter(ctx, base, token, after_seq):
    """Arm a background wake: a terminal command long-polls /wait and exits on
    the next event, waking Hermes via notify_on_complete."""
    cmd = f"{shlex.quote(str(_WAITER))} --base {shlex.quote(base)} --after {int(after_seq)}"
    return ctx.dispatch_tool("terminal", {
        "command": cmd,
        "background": True,
        "notify_on_complete": True,
        "env": {**os.environ, "NELIX_RPC": base, "NELIX_RPC_TOKEN": token},
    })
