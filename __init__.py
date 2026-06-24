import json
import logging
import os
from pathlib import Path

from .rpc_client import RpcClient
from .launcher_resolve import resolve_launcher
from .wake import arm_waiter
from . import supervisor, registry

_log = logging.getLogger("nelix")
_OBJ = {"type": "object", "additionalProperties": False}
_SKILLS_DIR = Path(__file__).parent / "skills"


def register(ctx):
    registry.seed_if_absent()

    def nelix_start(args, **k):
        # cwd is per-session: caller-supplied project dir, else this orchestrator's own
        # working dir (no static config cwd). The daemon resolves/validates it host-side.
        cwd = args.get("cwd") or os.getcwd()
        _log.info("nelix_start executor=%s cwd=%s", args["executor"], cwd)
        resolve_launcher("auto")               # isolation parity: fail closed
        base, token = supervisor.ensure_running()
        body = RpcClient(base, token).start(args["executor"], args["task"], cwd)
        arm_waiter(ctx, base, after_seq=0, token_file=supervisor.state_file())
        return json.dumps(body)

    def nelix_status(args, **k):
        bt = supervisor.base_token()
        if bt is None:
            return json.dumps({"sessions": {}})
        base, token = bt
        return json.dumps(RpcClient(base, token).status(args.get("session_id")))

    def nelix_respond(args, **k):
        _log.info("nelix_respond session=%s event=%s", args["session_id"], args.get("event_id"))
        bt = supervisor.base_token()
        if bt is None:
            return json.dumps({"error": "no active nelix daemon"})
        base, token = bt
        ok, body = RpcClient(base, token).respond(
            args["session_id"], args["event_id"], args["answer"])
        if ok:
            arm_waiter(ctx, base, after_seq=int(args.get("after_seq", 0)),
                       token_file=supervisor.state_file())
        return json.dumps(body)

    def nelix_stop(args, **k):
        bt = supervisor.base_token()
        if bt is None:
            return json.dumps({"stopped": False})
        base, token = bt
        return json.dumps(RpcClient(base, token).stop(args["session_id"]))

    def nelix_dialog(args, **k):
        bt = supervisor.base_token()
        if bt is None:
            return json.dumps({"error": "no active nelix daemon"})
        base, token = bt
        return json.dumps(RpcClient(base, token).dialog(
            args["session_id"], args.get("turn"), int(args.get("offset", 0)), args.get("limit")))

    names = ", ".join(registry.names()) or "the configured CLI"
    # Hermes builds the LLM tool spec as {"type":"function","function":{**schema,"name":name}}
    # (tools/registry.py), so `schema` MUST be the full function schema with `description`
    # and `parameters` nested — exactly like plugins/google_meet's MEET_JOIN_SCHEMA. A bare
    # parameters object + a `description=` kwarg is dropped: the LLM sees no description and
    # no parameters.
    ctx.register_tool(
        "nelix_start", "nelix",
        {"description": (
            f"Delegate a task to an agentic CLI executor ({names}) — an autonomous coding agent"
            " that works on its own and pauses only to ask permission or make a decision. Spawns"
            " the session, returns its session_id, and arms a wake-up for the next decision."
            " 'executor' is a configured name. Use this to hand dev work to the CLI instead of"
            " doing it yourself. 'cwd' is the working directory (project/repo) the executor"
            " runs in; omit it to use your own current working directory. Before orchestrating,"
            " you MUST call skill_view(\"nelix:nelix-orchestration\") to read the contract."),
         "parameters": {**_OBJ,
                        "properties": {"executor": {"type": "string"}, "task": {"type": "string"},
                                       "cwd": {"type": "string"}},
                        "required": ["executor", "task"]}},
        nelix_start)
    ctx.register_tool(
        "nelix_status", "nelix",
        {"description": (
            "Inspect an orchestrated executor: its current state and any decision it is blocked"
            " on awaiting your answer. Omit session_id to list active sessions. Call this ONCE"
            " each time the wake-up brings you back, to reconcile state — do NOT poll it in a loop"
            " while the executor runs (that wastes tokens; you sleep between decisions)."),
         "parameters": {**_OBJ, "properties": {"session_id": {"type": "string"}}}},
        nelix_status)
    ctx.register_tool(
        "nelix_respond", "nelix",
        {"description": (
            "Answer the decision a paused executor asked about (e.g. '1' to approve, or free text)"
            " so it resumes. Bound to that decision's exact event_id; pass the last-seen after_seq"
            " so the next wake-up fires on a new decision, not the one just answered."),
         "parameters": {**_OBJ,
                        "properties": {"session_id": {"type": "string"}, "event_id": {"type": "string"},
                                       "answer": {"type": "string"}, "after_seq": {"type": "integer"}},
                        "required": ["session_id", "event_id", "answer"]}},
        nelix_respond)
    ctx.register_tool(
        "nelix_stop", "nelix",
        {"description": "Terminate a running executor session by session_id.",
         "parameters": {**_OBJ, "properties": {"session_id": {"type": "string"}},
                        "required": ["session_id"]}},
        nelix_stop)
    ctx.register_tool(
        "nelix_dialog", "nelix",
        {"description": ("Read an orchestrated executor's dialog transcript: the latest turn by"
                         " default, or an earlier `turn` index, paginated by `offset`/`limit`. Use"
                         " to read a long question or review earlier turns the wake-up summarized."),
         "parameters": {**_OBJ, "properties": {
             "session_id": {"type": "string"}, "turn": {"type": "integer"},
             "offset": {"type": "integer"}, "limit": {"type": "integer"}},
             "required": ["session_id"]}},
        nelix_dialog)

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

    # on_session_finalize, NOT on_session_end: on_session_end fires at the end of every
    # run_conversation (i.e. every turn) + interrupted exits, which would tear the daemon
    # down mid-task the moment the agent correctly yields between decisions. finalize fires
    # only at true teardown (CLI exit / /new / /reset), so the daemon survives across turns
    # (required by the wake-between-decisions design). Verified against Hermes cli.py.
    ctx.register_hook("on_session_finalize",
                      lambda **kw: supervisor.teardown("session finalized"))
