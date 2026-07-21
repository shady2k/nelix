"""Argument surface only: this module wires subcommands to the functions that implement them and
owns `main`. Keeping the parser here (and the behavior in sibling modules) is what lets a verb be
added without touching any other verb's code."""
import argparse
import sys

from nelix_cli import daemon_cmds
from nelix_cli import rpc
from nelix_cli import wait_cmd


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nelix",
        description="nelix CLI: router lifecycle, the orchestration action contract, "
                    "the wake doorbell, and executor configuration.")
    top = parser.add_subparsers(dest="command", required=True)
    _add_daemon(top)
    rpc.add_parser(top)
    wait_cmd.add_parser(top)
    return parser


def _add_daemon(top) -> None:
    daemon = top.add_parser("daemon", help="router lifecycle, board reads, orchestration wait")
    sub = daemon.add_subparsers(dest="daemon_command", required=True)

    p_ensure = sub.add_parser(
        "ensure", help="ensure the router is running for this NELIX_HOME (idempotent)")
    p_ensure.set_defaults(func=daemon_cmds._cmd_ensure)

    p_status = sub.add_parser("status", help="print the router's owner-filtered board")
    p_status.add_argument("--owner", required=True, help="the owner_id to filter the board by")
    p_status.set_defaults(func=daemon_cmds._cmd_status)

    p_wait = sub.add_parser(
        "wait", help="arm the orchestration doorbell and print the one-shot result, then exit")
    p_wait.add_argument("--owner", required=True, help="the owner_id the wait is scoped to")
    p_wait.add_argument("--orchestration", required=True, help="the orchestration_id to wait on")
    p_wait.add_argument("--cursor", default=None,
                        help="opaque cursor token from a prior wait/status reply; "
                             "omit to arm from now")
    p_wait.set_defaults(func=daemon_cmds._cmd_wait)


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
