import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from .rpc_client import RpcClient
from .daemon.transport import Transport
from .launcher_resolve import resolve_launcher
from .wake import arm_waiter
from . import supervisor, registry

_log = logging.getLogger("nelix")
_OBJ = {"type": "object", "additionalProperties": False}
_SKILLS_DIR = Path(__file__).parent / "skills"

_dumps = json.dumps


def _j(obj):
    # Emit real UTF-8 (Cyrillic task text, ❯, …) to the model, not \uXXXX escapes.
    return _dumps(obj, ensure_ascii=False)


def _client(base, token):
    """Build an RpcClient from the (base_url, token) pair returned by supervisor."""
    u = urlparse(base)
    return RpcClient(Transport.tcp(u.hostname, u.port, token))


def register(ctx):
    registry.seed_if_absent()

    def nelix_start(args, **k):
        # cwd is per-session: caller-supplied project dir, else this orchestrator's own
        # working dir (no static config cwd). The daemon resolves/validates it host-side.
        cwd = args.get("cwd") or os.getcwd()
        _log.info("nelix_start executor=%s cwd=%s", args["executor"], cwd)
        # Config-first: a broken nelix.toml or a disabled executor is a relayable message, not a
        # spawned daemon + traceback. validate() reads the same file the daemon loads (single source).
        cfg_err = registry.config_error_for(registry.validate(), args["executor"])
        if cfg_err:
            _log.warning("nelix_start config error executor=%s", args["executor"])
            return _j(cfg_err)
        resolve_launcher("auto")               # isolation parity: fail closed
        base, token = supervisor.ensure_running()
        body = _client(base, token).start(args["executor"], args["task"], cwd)
        # The daemon owns the cursor: arm the wake past anything emitted before this session,
        # scoped to this session so cross-session events never produce a stale wake. Only arm on a
        # successful start — a failed start (e.g. bad cwd) has no session, so an unscoped waiter
        # would later wake on an unrelated session's event.
        if body.get("session_id"):
            arm_waiter(ctx, base, after_seq=int(body.get("next_after_seq", 0)),
                       session_id=body["session_id"], token_file=supervisor.state_file())
        return _j(body)

    def nelix_status(args, **k):
        bt = supervisor.base_token()
        if bt is None:
            return _j({"sessions": {}})
        base, token = bt
        return _j(_client(base, token).status(args.get("session_id")))

    def nelix_respond(args, **k):
        _log.info("nelix_respond session=%s decision=%s", args["session_id"],
                  args.get("decision_id"))
        bt = supervisor.base_token()
        if bt is None:
            return _j({"error": "no active nelix daemon"})
        base, token = bt
        # No event_id: the daemon binds the answer to the session's current pending decision.
        # decision_id (if the model carries it from a status pull) is an optional staleness guard.
        ok, body = _client(base, token).respond(
            args["session_id"], args["answer"], decision_id=args.get("decision_id"))
        if ok:
            # The daemon owns the cursor: arm the next doorbell past the decision we just answered.
            arm_waiter(ctx, base, after_seq=int(body.get("next_after_seq", 0)),
                       session_id=args["session_id"], token_file=supervisor.state_file())
        return _j(body)

    def nelix_stop(args, **k):
        bt = supervisor.base_token()
        if bt is None:
            return _j({"stopped": False})
        base, token = bt
        return _j(_client(base, token).stop(args["session_id"]))

    def nelix_dialog(args, **k):
        bt = supervisor.base_token()
        if bt is None:
            return _j({"error": "no active nelix daemon"})
        base, token = bt
        return _j(_client(base, token).dialog(
            args["session_id"], args.get("turn"), int(args.get("offset", 0)), args.get("limit")))

    def nelix_screen(args, **k):
        bt = supervisor.base_token()
        if bt is None:
            return _j({"error": "no active nelix daemon"})
        base, token = bt
        return _j(_client(base, token).screen(
            args["session_id"], raw=bool(args.get("raw")), force=bool(args.get("force"))))

    names = ", ".join(registry.names()) or "a configured agent"
    # Hermes builds the LLM tool spec as {"type":"function","function":{**schema,"name":name}}
    # (tools/registry.py), so `schema` MUST be the full function schema with `description`
    # and `parameters` nested — exactly like plugins/google_meet's MEET_JOIN_SCHEMA. A bare
    # parameters object + a `description=` kwarg is dropped: the LLM sees no description and
    # no parameters.
    ctx.register_tool(
        "nelix_start", "nelix",
        {"description": (
            f"Hand a coding/dev task to a named agent ({names}); it works on its own and pauses"
            " only for a decision or when done. Returns at once — you're brought back when it needs"
            " you or finishes, and spend nothing meanwhile. 'executor' is the agent's configured"
            " name; 'cwd' is the project dir it runs in (omit = your current dir). Before driving"
            " it, you MUST call skill_view(\"nelix:nelix-orchestration\")."),
         "parameters": {**_OBJ,
                        "properties": {"executor": {"type": "string"}, "task": {"type": "string"},
                                       "cwd": {"type": "string"}},
                        "required": ["executor", "task"]}},
        nelix_start)
    ctx.register_tool(
        "nelix_status", "nelix",
        {"description": (
            "Read an agent's current state and any pending decision (incl. its decision_id and the"
            " live screen). The wake is only a doorbell — on each wake call this ONCE to see what the"
            " agent needs, then act. Do NOT poll it in a loop: while the agent works it just says"
            " 'still working' and nelix wakes you on the next event. Omit session_id to list all."),
         "parameters": {**_OBJ, "properties": {"session_id": {"type": "string"}}}},
        nelix_status)
    ctx.register_tool(
        "nelix_respond", "nelix",
        {"description": (
            "Send the user's answer to a paused agent (e.g. '1' to approve, or free text) so it"
            " continues. It is delivered to the agent's CURRENT pending decision — you do NOT need"
            " an event id. (Optional: pass decision_id from a nelix_status read as a staleness"
            " guard.) After it succeeds, end your turn — nelix wakes you on the next event."),
         "parameters": {**_OBJ,
                        "properties": {"session_id": {"type": "string"}, "answer": {"type": "string"},
                                       "decision_id": {"type": "string"}},
                        "required": ["session_id", "answer"]}},
        nelix_respond)
    ctx.register_tool(
        "nelix_stop", "nelix",
        {"description": "Stop a running agent by session_id.",
         "parameters": {**_OBJ, "properties": {"session_id": {"type": "string"}},
                        "required": ["session_id"]}},
        nelix_stop)
    ctx.register_tool(
        "nelix_dialog", "nelix",
        {"description": ("Read an agent's transcript: the latest turn by default, or an earlier `turn`"
                         " index, paginated by `offset`/`limit`. For a long question or earlier turns —"
                         " not for polling progress."),
         "parameters": {**_OBJ, "properties": {
             "session_id": {"type": "string"}, "turn": {"type": "integer"},
             "offset": {"type": "integer"}, "limit": {"type": "integer"}},
             "required": ["session_id"]}},
        nelix_dialog)
    ctx.register_tool(
        "nelix_screen", "nelix",
        {"description": ("Fallback: the live terminal screen, when the wake's screen_excerpt isn't"
                         " enough — borders and framing stripped for readability. Not for polling"
                         " progress. While the agent works the screen is withheld with a brief 'still"
                         " working' note; force:true is the only way to see it anyway. raw:true is just"
                         " unfiltered formatting (and is still withheld while working unless force)."),
         "parameters": {**_OBJ, "properties": {"session_id": {"type": "string"},
                                               "raw": {"type": "boolean"},
                                               "force": {"type": "boolean"}},
                        "required": ["session_id"]}},
        nelix_screen)

    def slash_nelix(raw_args):
        raw = (raw_args or "").strip()
        if not raw or ":" not in raw:
            return "Usage: /nelix <agent>: <task>   (e.g. /nelix <agent>: fix the test)"
        executor, _, task = raw.partition(":")
        if not task.strip():
            return "Usage: /nelix <agent>: <task>"
        return nelix_start({"executor": executor.strip(), "task": task.strip()})

    ctx.register_command(
        "nelix", slash_nelix,
        description="Hand a task to a named coding agent and drive it. Usage: /nelix <agent>: <task>",
        args_hint="<agent>: <task>")

    ctx.register_skill(
        "nelix-orchestration", _SKILLS_DIR / "nelix-orchestration" / "SKILL.md",
        description=("Drive a named coding agent via the nelix_* tools as a companion — hold the"
                     " goal, recover, and bring real decisions to the user."))

    # on_session_finalize, NOT on_session_end: on_session_end fires at the end of every
    # run_conversation (i.e. every turn) + interrupted exits, which would tear the daemon
    # down mid-task the moment the agent correctly yields between decisions. finalize fires
    # only at true teardown (CLI exit / /new / /reset), so the daemon survives across turns
    # (required by the wake-between-decisions design). Verified against Hermes cli.py.
    ctx.register_hook("on_session_finalize",
                      lambda **kw: supervisor.teardown("session finalized"))
