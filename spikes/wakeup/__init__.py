import json


def register(ctx):
    def _arm(args, **kwargs):
        secs = int(args.get("seconds", 20))
        # Self-contained command so it runs in the Dockerized terminal backend with no
        # host file dependency. On exit, notify_on_complete should wake Hermes with stdout.
        out = ctx.dispatch_tool("terminal", {
            "command": f"sleep {secs}; echo nelix_event spikeA evt-$(date +%s)",
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
