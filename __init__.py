import json
import logging
import os
from pathlib import Path

from .rpc_client import RpcClient
from .launcher_resolve import resolve_launcher
from .wake import arm_waiter
from . import supervisor, registry
try:
    from .nelix_cursor import WakeRegistry
except ImportError:
    from nelix_cursor import WakeRegistry

_log = logging.getLogger("nelix")
_OBJ = {"type": "object", "additionalProperties": False}
_SKILLS_DIR = Path(__file__).parent / "skills"

_dumps = json.dumps


def _j(obj):
    # Emit real UTF-8 (Cyrillic task text, ❯, …) to the model, not \uXXXX escapes.
    return _dumps(obj, ensure_ascii=False)


def register(ctx):
    registry.seed_if_absent()

    waiters = WakeRegistry()        # one wake cursor + arm-dedup PER active session

    def _is_terminal(snap):
        # A session is terminal once the daemon stamps a terminal_kind on it
        # (done/crashed/stopped/delivery_failed). Key on the FLAG, never on enumerated state
        # strings: a clean exit reports state="exited" (not "done"), so a state allowlist would
        # miss it and re-arm a waiter on a dead session that emits no further events. Reconcile-
        # by-absence (below) is the backstop; this closes the publish-to-free live window.
        return bool(snap.get("terminal_kind"))

    def _daemon_id():
        # Identity of the live daemon (its pid from the supervisor state file), so the registry
        # resets reliably across a daemon teardown/restart (not via a fragile seq heuristic).
        try:
            with open(supervisor.state_file()) as f:
                return json.load(f).get("pid")
        except (OSError, ValueError):
            return None

    def _arm(sid):
        # Arm/re-arm exactly one waiter for THIS session. claim_arm is atomic (check + mark);
        # dispatch outside the lock. Returns the after_seq armed at, or None if no waiter dispatched.
        after = waiters.claim_arm(sid)
        if after is not None:
            arm_waiter(ctx, after_seq=after, state_file=supervisor.state_file(), session_id=sid)
        return after

    def _with_waiter(body, armed_after):
        armed = armed_after is not None
        body["waiter"] = {"armed": armed,
                          "after_seq": armed_after if armed else int(body.get("next_after_seq", 0))}
        if not armed and body.get("next_action") == "end_turn":
            body["next_action"] = "refresh_status"   # success expected an arm but none happened -> reconcile
        return body

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
        transport = supervisor.ensure_running()
        # model (nelix-9k0): optional per-session executor model override, threaded to the wire only
        # when provided (RpcClient.start omits it from the body otherwise).
        body = RpcClient(transport).start(args["executor"], args["task"], cwd,
                                          model=args.get("model"))
        # Register this new session's base cursor, then arm one waiter scoped to it. Only arm on a
        # successful start — a failed start (e.g. bad cwd) has no session and must not arm a waiter.
        armed_after = None
        if body.get("session_id"):
            sid = body["session_id"]
            waiters.on_start(sid, int(body.get("next_after_seq", 0)), daemon_id=_daemon_id())
            armed_after = _arm(sid)
        return _j(_with_waiter(body, armed_after))

    def nelix_status(args, **k):
        transport = supervisor.endpoint()
        if transport is None:
            return _j({"sessions": {}})
        sid = args.get("session_id")
        include_progress = bool(args.get("include_progress", False))
        body = RpcClient(transport).status(sid, include_progress=include_progress)
        if sid is None:
            # ALL-SESSIONS board read (the path SKILL.md tells Hermes to use on every wake):
            # reconcile every session, then drop any registry entry not on the live board.
            if isinstance(body, dict):
                live = body.get("sessions", {}) or {}
                for s_id, snap in live.items():
                    if _is_terminal(snap):
                        waiters.drop(s_id)
                        continue
                    seq = max(int(snap.get("seq", 0)),
                              int((snap.get("decision") or {}).get("seq", 0)))
                    waiters.on_status(s_id, seq)
                    _arm(s_id)
                for s_id in list(waiters.active_sids()):
                    if s_id not in live:            # terminal / recent_terminal / gone
                        waiters.drop(s_id)
        else:
            # PER-SESSION read (secondary trigger): reconcile just this session.
            if isinstance(body, dict) and "error" not in body \
                    and not _is_terminal(body):
                seq = max(int(body.get("cursor", 0)),
                          int((body.get("decision") or {}).get("seq", 0)))
                waiters.on_status(sid, seq)
                _arm(sid)
            else:
                waiters.drop(sid)
        return _j(body)

    def nelix_respond(args, **k):
        _log.info("nelix_respond session=%s decision=%s", args["session_id"],
                  args.get("decision_id"))
        transport = supervisor.endpoint()
        if transport is None:
            return _j({"error": "no active nelix daemon"})
        # The daemon binds the answer to the session's CURRENT pending decision, but ONLY if the
        # caller names it: decision_id (from a nelix_status read) is REQUIRED to answer a pending
        # question — omit it solely for an idle follow-up (manager.respond routes idle → send_turn,
        # which never reaches the missing_decision_id guard). On missing_decision_id the daemon
        # returns 409 with the pending decision so the caller retries without another status pull.
        # RpcClient.respond returns (ok, body) where ok = (st == 200); write_timeout is HTTP 503
        # → ok=False → no arm.
        ok, body = RpcClient(transport).respond(
            args["session_id"], args["answer"], decision_id=args.get("decision_id"))
        armed_after = None
        if ok and body.get("status") == "resumed":
            waiters.on_respond(args["session_id"], int(body.get("next_after_seq", 0)))
            armed_after = _arm(args["session_id"])
        return _j(_with_waiter(body, armed_after))

    def nelix_stop(args, **k):
        transport = supervisor.endpoint()
        if transport is None:
            return _j({"error": "no active nelix daemon"})
        sid = args["session_id"]
        body = RpcClient(transport).stop(sid)
        status = body.get("status") if isinstance(body, dict) else None
        armed_after = None
        if status == "stop_requested":
            # Teardown not yet confirmed: keep the session in the registry so the eventual
            # terminal event wakes the orchestrator. Arm (or re-arm) the waiter; if
            # claim_arm returns None, a waiter is already pending — fall back to the
            # registry's current cursor so waiter.armed is truthfully reported.
            armed_after = _arm(sid)
            if armed_after is None:
                armed_after = waiters.value(sid)   # non-None if session tracked, else None
        elif status == "stopped":
            waiters.drop(sid)
        return _j(_with_waiter(body, armed_after))

    def nelix_dialog(args, **k):
        transport = supervisor.endpoint()
        if transport is None:
            return _j({"error": "no active nelix daemon"})
        return _j(RpcClient(transport).dialog(
            args["session_id"], int(args.get("offset", 0)), args.get("limit")))

    def nelix_restart(args, **k):
        _log.info("nelix_restart session=%s force=%s", args["session_id"], args.get("force"))
        transport = supervisor.endpoint()
        if transport is None:
            return _j({"error": "no active nelix daemon"})
        ok, body = RpcClient(transport).restart(args["session_id"], force=bool(args.get("force", False)))
        armed_after = None
        if ok and body.get("status") == "restarted":
            waiters.drop(args["session_id"])                 # old session gone
            new_sid = body.get("session_id")
            if new_sid:
                waiters.on_start(new_sid, int(body.get("next_after_seq", 0)), daemon_id=_daemon_id())
                armed_after = _arm(new_sid)
        return _j(_with_waiter(body, armed_after))

    def nelix_screen(args, **k):
        transport = supervisor.endpoint()
        if transport is None:
            return _j({"error": "no active nelix daemon"})
        return _j(RpcClient(transport).screen(
            args["session_id"], raw=bool(args.get("raw")), force=bool(args.get("force"))))

    def nelix_models(args, **k):
        # nelix-g9k: read-only model discovery. Config-first (mirrors nelix_start): a broken
        # nelix.toml or a disabled executor is a relayable message, not a spawned daemon + traceback.
        executor = args["executor"]
        _log.info("nelix_models executor=%s", executor)
        cfg_err = registry.config_error_for(registry.validate(), executor)
        if cfg_err:
            _log.warning("nelix_models config error executor=%s", executor)
            return _j(cfg_err)
        # The executor specs + resolver live in the daemon, so ensure it's running (mirrors
        # nelix_start). RpcClient.models returns (status, body); the tool RELAYS the body — a model
        # list on 200, or the clean {error} on 400/404/502 — so the status itself is not needed here.
        transport = supervisor.ensure_running()
        _, body = RpcClient(transport).models(executor)
        return _j(body)

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
            " it, you MUST call skill_view(\"nelix:nelix-orchestration\")."
            " The returned result is the COMPLETE outcome of this call — obey its `next_action`"
            " (`end_turn` → stop and wait to be woken; `report` → relay to the user;"
            " `ask_user`/`fix_call`/`recover`/`refresh_status` → act accordingly)."
            " Do NOT call nelix_status after this."),
         "parameters": {**_OBJ,
                        "properties": {"executor": {"type": "string"}, "task": {"type": "string"},
                                       "cwd": {"type": "string"},
                                       "model": {"type": "string", "description": (
                                           "Optional per-session model. Accepts whatever the"
                                           " executor's CLI accepts — a tier alias"
                                           " (haiku/sonnet/opus) or a full model id; omit for the"
                                           " executor's configured default.")}},
                        "required": ["executor", "task"]}},
        nelix_start)
    ctx.register_tool(
        "nelix_status", "nelix",
        {"description": (
            "Read an agent's current state and any pending decision (incl. its decision_id and the"
            " live screen). The wake is only a doorbell — on each wake call this ONCE to see what the"
            " agent needs, then act. Do NOT poll it in a loop: while the agent works it just says"
            " 'still working' and nelix wakes you on the next event. Omit session_id to list all."
            " A snapshot may carry `async_question` — a NON-blocking question the agent asked WHILE"
            " it kept working (`executor_blocked:false`; it did not pause, unlike `decision`)."
            " Answer it with nelix_respond, passing its `id` as decision_id; if the agent finishes"
            " before your answer arrives, nelix_respond reports `not_delivered` instead of resuming"
            " it. `include_progress:true` also returns the agent's bounded progress-note list (its"
            " own non-waking status updates) even while it's still working, for when you need that"
            " detail on demand — omit it (the default) to keep the normal poll-free 'still working'"
            " view with nothing extra to read."),
         "parameters": {**_OBJ, "properties": {"session_id": {"type": "string"},
                                               "include_progress": {"type": "boolean"}}}},
        nelix_status)
    ctx.register_tool(
        "nelix_respond", "nelix",
        {"description": (
            "Send the user's answer to a paused agent (e.g. '1' to approve, or free text) so it"
            " continues. To answer a pending question you MUST pass `decision_id` (from your"
            " nelix_status read): it names the decision this answer binds to. Omit `decision_id`"
            " ONLY for an idle follow-up (a new instruction to an already-idle agent). If you omit"
            " it on a pending question the daemon returns `missing_decision_id` with that pending"
            " decision — retry using it (no separate nelix_status call). After it succeeds, end your"
            " turn — nelix wakes you on the next event."
            " `decision_id` may also name an outstanding `async_question.id` (a non-blocking question"
            " the agent asked while still working, from a status snapshot) — answering that delivers"
            " your answer as a fresh follow-up turn, not a keystroke into a paused modal. The outcome"
            " depends on the agent's state when your answer lands. If it is still busy (the usual case),"
            " expect `status:\"queued\"`: accepted and delivered when the agent next goes idle —"
            " `next_action` is `refresh_status`, not `end_turn`. If the agent had already gone idle in"
            " the meantime (it asked, then finished its turn before you answered), the answer is"
            " delivered IMMEDIATELY as that fresh turn and you get `status:\"resumed\"` — the normal"
            " resumed envelope (`next_action:\"end_turn\"`, same as an idle follow-up), NOT `queued`;"
            " report it as delivered, end your turn, and nelix wakes you on the next event. If instead"
            " the agent already finished/exited before your answer got there, expect"
            " `status:\"not_delivered\"` (`reason:\"executor_finished\"`) instead of `resumed`: nothing"
            " was sent — its `next_action` is also `refresh_status`, so reconcile via nelix_status and"
            " report the agent's actual outcome to the user."
            " The returned result is the COMPLETE outcome of this call — obey its `next_action`"
            " (`end_turn` → stop and wait to be woken; `report` → relay to the user;"
            " `ask_user`/`fix_call`/`recover`/`refresh_status` → act accordingly)."
            " Do NOT call nelix_status after this."),
         "parameters": {**_OBJ,
                        "properties": {"session_id": {"type": "string"}, "answer": {"type": "string"},
                                       "decision_id": {"type": "string"}},
                        "required": ["session_id", "answer"]}},
        nelix_respond)
    ctx.register_tool(
        "nelix_stop", "nelix",
        {"description": ("Stop a running agent by session_id."
                         " The returned result is the COMPLETE outcome of this call — obey its"
                         " `next_action` (`report` → stop confirmed, relay to the user;"
                         " `refresh_status` → teardown still in progress, reconcile via status;"
                         " you will also be woken when the process stops)."
                         " Do NOT call nelix_status after this."),
         "parameters": {**_OBJ, "properties": {"session_id": {"type": "string"}},
                        "required": ["session_id"]}},
        nelix_stop)
    ctx.register_tool(
        "nelix_restart", "nelix",
        {"description": (
            "Restart a crashed or wedged agent by session_id — one call, reusing its original task,"
            " project, and agent (you do NOT re-state the task). The daemon counts restarts per agent"
            " and refuses past its max_restarts with 'restart_budget_exhausted'; relay that to the user"
            " and only pass force:true if they explicitly authorize continuing. After it succeeds, end"
            " your turn — nelix wakes you on the next event."
            " The returned result is the COMPLETE outcome of this call — obey its `next_action`"
            " (`end_turn` → stop and wait to be woken; `report` → relay to the user;"
            " `ask_user`/`fix_call`/`recover`/`refresh_status` → act accordingly)."
            " Do NOT call nelix_status after this."),
         "parameters": {**_OBJ, "properties": {"session_id": {"type": "string"},
                                               "force": {"type": "boolean"}},
                        "required": ["session_id"]}},
        nelix_restart)
    ctx.register_tool(
        "nelix_dialog", "nelix",
        {"description": ("Read the agent's transcript by pages from an offset; each page says whose"
                         " output it starts with (speaker_at_start); use next_offset to continue."
                         " For a long session or reading from last_user_input_offset — not for"
                         " polling progress."),
         "parameters": {**_OBJ, "properties": {
             "session_id": {"type": "string"},
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
    ctx.register_tool(
        "nelix_models", "nelix",
        {"description": (
            f"List a named agent's ({names}) available models by running its configured"
            " model-discovery command; returns the raw text the command prints (typically one model"
            " id + display name per line). Use it when you need a CONCRETE model id to pass as"
            " nelix_start's `model` (beyond the tier aliases). Read-only: it starts no session and"
            " changes nothing. 'executor' is the agent's configured name. If that agent has no"
            " model-discovery command configured you get a clear 'not configured' error — relay it"
            " and do not retry."),
         "parameters": {**_OBJ, "properties": {"executor": {"type": "string"}},
                        "required": ["executor"]}},
        nelix_models)

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
