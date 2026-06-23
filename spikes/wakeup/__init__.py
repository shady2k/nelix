import json
import os


def register(ctx):
    def _arm(args, **kwargs):
        secs = int(args.get("seconds", 20))
        waiter = os.path.join(os.path.dirname(__file__), "waiter.sh")
        out = ctx.dispatch_tool("terminal", {
            "command": f"bash {waiter} {secs}",
            "background": True,
            "notify_on_complete": True,
        })
        return json.dumps({"status": "armed", "seconds": secs, "terminal": out})

    ctx.register_tool(
        name="nelix_spike_arm", toolset="nelix",
        schema={"name": "nelix_spike_arm",
                "description": "Arm a background waiter; expect a wake-up turn ~N seconds later.",
                "parameters": {"type": "object", "properties": {"seconds": {"type": "integer"}}}},
        handler=_arm,
    )
