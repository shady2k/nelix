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
