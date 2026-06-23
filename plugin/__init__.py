import json
import os

from plugin.rpc_client import RpcClient
from plugin.launcher_resolve import resolve_launcher
from plugin.wake import arm_waiter

_OBJ = {"type": "object", "additionalProperties": False}


def _client():
    return RpcClient(os.environ.get("NELIX_RPC", "http://127.0.0.1:8765"),
                     os.environ["NELIX_RPC_TOKEN"])


def register(ctx):
    base = os.environ.get("NELIX_RPC", "http://127.0.0.1:8765")

    def nelix_start(args, **k):
        # Isolation parity: fail closed before doing anything (raises if weaker/post-MVP).
        resolve_launcher("auto")
        body = _client().start(args["executor"], args["task"])
        arm_waiter(ctx, base, after_seq=0)
        return json.dumps(body)

    def nelix_status(args, **k):
        return json.dumps(_client().status(args.get("session_id")))

    def nelix_respond(args, **k):
        ok, body = _client().respond(args["session_id"], args["event_id"], args["answer"])
        # Re-arm the waiter so the next decision wakes us again.
        if ok:
            arm_waiter(ctx, base, after_seq=int(args.get("after_seq", 0)))
        return json.dumps(body)

    def nelix_stop(args, **k):
        return json.dumps(_client().stop(args["session_id"]))

    ctx.register_tool("nelix_start", "nelix",
                      {**_OBJ, "properties": {"executor": {"type": "string"},
                                              "task": {"type": "string"}},
                       "required": ["executor", "task"]},
                      nelix_start, description="Start orchestrating a configured executor on a task.")
    ctx.register_tool("nelix_status", "nelix",
                      {**_OBJ, "properties": {"session_id": {"type": "string"}}},
                      nelix_status, description="Status of one session, or list all active sessions.")
    ctx.register_tool("nelix_respond", "nelix",
                      {**_OBJ, "properties": {"session_id": {"type": "string"},
                                              "event_id": {"type": "string"},
                                              "answer": {"type": "string"},
                                              "after_seq": {"type": "integer"}},
                       "required": ["session_id", "event_id", "answer"]},
                      nelix_respond, description="Answer a session's pending decision (bound to event_id).")
    ctx.register_tool("nelix_stop", "nelix",
                      {**_OBJ, "properties": {"session_id": {"type": "string"}},
                       "required": ["session_id"]},
                      nelix_stop, description="Stop a running session.")

    def slash_nelix(raw_args):
        raw = (raw_args or "").strip()
        if not raw:
            return "Usage: /nelix <executor>: <task>   (e.g. /nelix claude_zai: fix the test)"
        executor, _, task = raw.partition(":")
        if not task.strip():
            return "Usage: /nelix <executor>: <task>"
        return nelix_start({"executor": executor.strip(), "task": task.strip()})

    ctx.register_command("nelix", slash_nelix,
                         description="Start a Nelix session: /nelix <executor>: <task>",
                         args_hint="<executor>: <task>")
