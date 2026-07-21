"""`nelix rpc <verb>` — the cli_api v1 action contract (spec §3.1).

Arguments are FLAGS, never a JSON blob the caller assembles: that is what removes shell quoting and
JSON escaping as a failure mode. The verbs are a thin, uniform mapping onto rpc_client.RpcClient —
the same client the router itself uses — so this module holds no protocol knowledge of its own.
"""
import paths
from daemon.transport import Transport
from nelix_cli import daemon_cmds
from nelix_cli.envelope import (EXIT_REJECTED, EXIT_UNAVAILABLE, ROUTER_ERRORS,
                                emit_error, emit_ok)
from rpc_client import RpcClient

import sys
import uuid

from nelix_contracts.ids import new_orchestration_id
from nelix_cli.envelope import EXIT_USAGE

_NO_ROUTER = "no router is running for this NELIX_HOME; run `nelix daemon ensure` first"


def client_for(owner_id: str) -> RpcClient:
    return RpcClient(Transport.unix(str(paths.router_sock())), owner_id)


def _call(fn):
    """Run one router call, mapping the two failure families onto their exit classes: no router
    answering /health -> UNAVAILABLE (the operator must start one), anything the router itself
    refuses or a dropped connection -> REJECTED (the request was wrong, not the installation)."""
    if daemon_cmds._router_health() is None:
        return emit_error("router_unavailable", _NO_ROUTER, exit_class=EXIT_UNAVAILABLE)
    try:
        body = fn()
    except ROUTER_ERRORS as e:
        return emit_error("router_call_failed", f"router call failed: {e}",
                          exit_class=EXIT_REJECTED)
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        code = err.get("code", "rejected") if isinstance(err, dict) else "rejected"
        message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        return emit_error(code, message, exit_class=EXIT_REJECTED)
    return emit_ok(body if isinstance(body, dict) else {"result": body})


def cmd_status(args) -> int:
    return _call(lambda: client_for(args.owner).status(
        session_id=args.session, include_progress=args.progress))


def cmd_dialog(args) -> int:
    return _call(lambda: client_for(args.owner).dialog(
        args.session, offset=args.offset, limit=args.limit))


def cmd_screen(args) -> int:
    return _call(lambda: client_for(args.owner).screen(
        args.session, raw=args.raw, force=args.force))


def _post(owner_id: str, path: str, payload: dict) -> dict:
    """POST to one of the ROUTER's own routes. Deliberately not RpcClient.start/.restart: those
    build the request shape the router sends to a GENERATION, which is not what these routes
    accept (/start requires idempotency_key; /restart assigns its own new session id)."""
    _status, body = client_for(owner_id)._call("POST", path, payload)
    return body


def read_text(path: str) -> str:
    """Free text for a verb: `-` reads stdin, anything else reads that file. UTF-8, returned
    EXACTLY as stored — no strip, no newline normalization. A brief is data, not a shell word."""
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _text_or_usage(path: str):
    """(text, None) or (None, exit-code): an unreadable input is the CALLER's mistake (a wrong
    path), so it is a usage error, never 'the router is unavailable'."""
    try:
        return read_text(path), None
    except OSError as e:
        return None, emit_error("unreadable_input", f"cannot read {path}: {e}",
                                exit_class=EXIT_USAGE)


def cmd_start(args) -> int:
    task, failure = _text_or_usage(args.task_file)
    if failure is not None:
        return failure
    orchestration_id = args.orchestration or new_orchestration_id()
    payload = {"owner_id": args.owner,
               "idempotency_key": args.idempotency_key or uuid.uuid4().hex,
               "orchestration_id": orchestration_id,
               "executor": args.executor, "task": task, "cwd": args.cwd}
    if args.model is not None:
        payload["model"] = args.model

    def _do():
        body = _post(args.owner, "/start", payload)
        # Echo the orchestration id we supplied: /start's reply does not carry it, and it is what
        # the caller must hand to `nelix wait`.
        if isinstance(body, dict) and not body.get("error"):
            return {**body, "orchestration_id": orchestration_id}
        return body

    return _call(_do)


def cmd_respond(args) -> int:
    answer, failure = _text_or_usage(args.answer_file)
    if failure is not None:
        return failure

    def _do():
        _ok, body = client_for(args.owner).respond(
            args.session, answer, decision_id=args.decision_id)
        return body

    return _call(_do)


def cmd_stop(args) -> int:
    return _call(lambda: client_for(args.owner).stop(args.session))


def cmd_restart(args) -> int:
    return _call(lambda: _post(args.owner, "/restart",
                               {"owner_id": args.owner, "session_id": args.session,
                                "force": args.force}))


def add_parser(top) -> None:
    rpc = top.add_parser("rpc", help="the cli_api v1 action contract (one JSON object on stdout)")
    sub = rpc.add_subparsers(dest="rpc_verb", required=True)

    p = sub.add_parser("status", help="read the board, or one session")
    p.add_argument("--owner", required=True)
    p.add_argument("--session", default=None)
    p.add_argument("--progress", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("dialog", help="read a session's transcript page")
    p.add_argument("--owner", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_dialog)

    p = sub.add_parser("screen", help="read a session's current screen")
    p.add_argument("--owner", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--raw", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_screen)

    p = sub.add_parser("start", help="start a worker; the brief comes from a file or stdin")
    p.add_argument("--owner", required=True)
    p.add_argument("--executor", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--task-file", dest="task_file", required=True,
                   help="path to the task brief, or `-` for stdin")
    p.add_argument("--orchestration", default=None,
                   help="join an existing orchestration; omit to mint a new one")
    p.add_argument("--idempotency-key", dest="idempotency_key", default=None,
                   help="retry the SAME start safely; omit for a fresh key")
    p.add_argument("--model", default=None)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("respond", help="answer a pending decision")
    p.add_argument("--owner", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--answer-file", dest="answer_file", required=True,
                   help="path to the answer text, or `-` for stdin")
    p.add_argument("--decision-id", dest="decision_id", default=None)
    p.set_defaults(func=cmd_respond)

    p = sub.add_parser("stop", help="stop a session")
    p.add_argument("--owner", required=True)
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("restart", help="restart a session, reusing its persisted task")
    p.add_argument("--owner", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_restart)
