import os
import shlex


def arm_waiter(ctx, base, after_seq, waiter_path=None):
    """Arm a background wake: a terminal command that long-polls /wait and exits
    on the next event, which wakes Hermes via notify_on_complete."""
    waiter_path = waiter_path or os.environ.get("NELIX_WAITER", "bin/nelix-wait")
    cmd = f"{shlex.quote(waiter_path)} --base {shlex.quote(base)} --after {int(after_seq)}"
    return ctx.dispatch_tool("terminal", {
        "command": cmd,
        "background": True,
        "notify_on_complete": True,
    })
