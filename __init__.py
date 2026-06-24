import json
from pathlib import Path

from .rpc_client import RpcClient
from .launcher_resolve import resolve_launcher
from .wake import arm_waiter
from . import supervisor, registry

_OBJ = {"type": "object", "additionalProperties": False}
_SKILLS_DIR = Path(__file__).parent / "skills"


def register(ctx):
    registry.seed_if_absent()

    def nelix_start(args, **k):
        resolve_launcher("auto")               # isolation parity: fail closed
        base, token = supervisor.ensure_running()
        body = RpcClient(base, token).start(args["executor"], args["task"])
        arm_waiter(ctx, base, token, after_seq=0)
        return json.dumps(body)

    def nelix_status(args, **k):
        bt = supervisor.base_token()
        if bt is None:
            return json.dumps({"sessions": {}})
        base, token = bt
        return json.dumps(RpcClient(base, token).status(args.get("session_id")))

    def nelix_respond(args, **k):
        bt = supervisor.base_token()
        if bt is None:
            return json.dumps({"error": "no active nelix daemon"})
        base, token = bt
        ok, body = RpcClient(base, token).respond(
            args["session_id"], args["event_id"], args["answer"])
        if ok:
            arm_waiter(ctx, base, token, after_seq=int(args.get("after_seq", 0)))
        return json.dumps(body)

    def nelix_stop(args, **k):
        bt = supervisor.base_token()
        if bt is None:
            return json.dumps({"stopped": False})
        base, token = bt
        return json.dumps(RpcClient(base, token).stop(args["session_id"]))

    names = ", ".join(registry.names()) or "the configured CLI"
    ctx.register_tool(
        "nelix_start", "nelix",
        {**_OBJ, "properties": {"executor": {"type": "string"}, "task": {"type": "string"}},
         "required": ["executor", "task"]},
        nelix_start,
        description=(
            f"Delegate a task to an agentic CLI executor ({names}) — an autonomous coding agent"
            " that works on its own and pauses only to ask permission or make a decision. Spawns"
            " the session, returns its session_id, and arms a wake-up for the next decision."
            " 'executor' is a configured name. Use this to hand dev work to the CLI instead of"
            " doing it yourself. Before orchestrating, you MUST call"
            " skill_view(\"nelix:nelix-orchestration\") to read the turn-by-turn contract."))
    ctx.register_tool(
        "nelix_status", "nelix",
        {**_OBJ, "properties": {"session_id": {"type": "string"}}},
        nelix_status,
        description=(
            "Inspect an orchestrated executor: its current state and any decision it is blocked"
            " on awaiting your answer. Omit session_id to list active sessions. Call this every"
            " turn while a session is active so a missed wake-up is recovered."))
    ctx.register_tool(
        "nelix_respond", "nelix",
        {**_OBJ, "properties": {"session_id": {"type": "string"}, "event_id": {"type": "string"},
                                "answer": {"type": "string"}, "after_seq": {"type": "integer"}},
         "required": ["session_id", "event_id", "answer"]},
        nelix_respond,
        description=(
            "Answer the decision a paused executor asked about (e.g. '1' to approve, or free text)"
            " so it resumes. Bound to that decision's exact event_id; pass the last-seen after_seq"
            " so the next wake-up fires on a new decision, not the one just answered."))
    ctx.register_tool(
        "nelix_stop", "nelix",
        {**_OBJ, "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
        nelix_stop,
        description="Terminate a running executor session by session_id.")

    def slash_nelix(raw_args):
        raw = (raw_args or "").strip()
        if not raw or ":" not in raw:
            return "Usage: /nelix <executor>: <task>   (e.g. /nelix opencode: fix the test)"
        executor, _, task = raw.partition(":")
        if not task.strip():
            return "Usage: /nelix <executor>: <task>"
        return nelix_start({"executor": executor.strip(), "task": task.strip()})

    ctx.register_command(
        "nelix", slash_nelix,
        description="Delegate a task to an agentic CLI executor and orchestrate it."
                    " Usage: /nelix <executor>: <task>",
        args_hint="<executor>: <task>")

    ctx.register_skill(
        "nelix-orchestration", _SKILLS_DIR / "nelix-orchestration" / "SKILL.md",
        description=("How to delegate a coding/dev task to an agentic CLI executor via the nelix_*"
                     " tools — drive it and relay its decisions to the user."))

    ctx.register_hook("on_session_end", lambda **kw: supervisor.teardown("session ended"))
