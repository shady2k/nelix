import json
import os
from pathlib import Path

from plugin.rpc_client import RpcClient
from plugin.launcher_resolve import resolve_launcher
from plugin.wake import arm_waiter

_OBJ = {"type": "object", "additionalProperties": False}
_SKILLS_DIR = Path(__file__).parent / "skills"


def _client():
    return RpcClient(os.environ.get("NELIX_RPC", "http://127.0.0.1:8765"),
                     os.environ["NELIX_RPC_TOKEN"])


def _executor_names():
    try:
        from daemon.config import load_executors
        names = sorted(load_executors(os.environ.get("NELIX_CONFIG", "nelix.toml")))
        return ", ".join(names) if names else "the configured CLI"
    except Exception:
        return "the configured CLI"


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

    names = _executor_names()
    ctx.register_tool("nelix_start", "nelix",
                      {**_OBJ, "properties": {"executor": {"type": "string"},
                                              "task": {"type": "string"}},
                       "required": ["executor", "task"]},
                      nelix_start,
                      description=(
                          f"Delegate a task to an agentic CLI executor ({names}) — an autonomous coding"
                          " agent that works on its own and pauses only to ask permission or make a"
                          " decision. Spawns the session, returns its session_id, and arms a wake-up"
                          " for the next decision. 'executor' is the configured name. Use this to hand"
                          " dev work to the CLI instead of doing it yourself."
                      ))
    ctx.register_tool("nelix_status", "nelix",
                      {**_OBJ, "properties": {"session_id": {"type": "string"}}},
                      nelix_status,
                      description=(
                          "Inspect an orchestrated executor: its current state and any decision it is"
                          " blocked on awaiting your answer. Omit session_id to list all active"
                          " sessions. Call this every turn while a session is active so a missed"
                          " wake-up is recovered."
                      ))
    ctx.register_tool("nelix_respond", "nelix",
                      {**_OBJ, "properties": {"session_id": {"type": "string"},
                                              "event_id": {"type": "string"},
                                              "answer": {"type": "string"},
                                              "after_seq": {"type": "integer"}},
                       "required": ["session_id", "event_id", "answer"]},
                      nelix_respond,
                      description=(
                          "Answer the decision a paused executor asked about (e.g. '1' to approve, or"
                          " free text) so it resumes. Bound to that decision's exact event_id; pass"
                          " the last-seen after_seq so the next wake-up fires on a new decision, not"
                          " the one just answered."
                      ))
    ctx.register_tool("nelix_stop", "nelix",
                      {**_OBJ, "properties": {"session_id": {"type": "string"}},
                       "required": ["session_id"]},
                      nelix_stop,
                      description="Terminate a running executor session by session_id.")

    def slash_nelix(raw_args):
        raw = (raw_args or "").strip()
        if not raw:
            return "Usage: /nelix <executor>: <task>   (e.g. /nelix claude_zai: fix the test)"
        executor, _, task = raw.partition(":")
        if not task.strip():
            return "Usage: /nelix <executor>: <task>"
        return nelix_start({"executor": executor.strip(), "task": task.strip()})

    ctx.register_command("nelix", slash_nelix,
                         description="Delegate a task to an agentic CLI executor and orchestrate it."
                                     " Usage: /nelix <executor>: <task>",
                         args_hint="<executor>: <task>")

    ctx.register_skill(
        "nelix-orchestration",
        _SKILLS_DIR / "nelix-orchestration" / "SKILL.md",
        description=(
            "How to delegate a coding/dev task to an agentic CLI executor via the nelix_* tools"
            " — drive it and relay its decisions to the user. Use whenever a nelix session is active."
        ),
    )
