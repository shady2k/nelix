"""`nelix launcher install|show` — install or inspect the stable launcher.

The bootstrapper installs the launcher as its last step; this verb is that same operation, exposed
so an operator can repair a launcher without a reinstall and so the installer has exactly one
implementation.
"""
from pathlib import Path

import launcher
import paths
from nelix_cli.envelope import EXIT_REJECTED, emit_error, emit_ok


def _home(args):
    return args.home or str(paths.nelix_root())


def cmd_install(args) -> int:
    path = launcher.install(_home(args))
    return emit_ok({"path": str(path), "home": _home(args)})


def cmd_show(args) -> int:
    path = Path(_home(args)) / "bin" / "nelix"
    if not path.exists():
        return emit_error("launcher_absent",
                          f"no launcher at {path} — run `nelix launcher install`",
                          exit_class=EXIT_REJECTED)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        return emit_error("launcher_unreadable", f"cannot read {path}: {e}",
                          exit_class=EXIT_REJECTED)
    return emit_ok({"path": str(path), "home": _home(args),
                    "current": content == launcher.DISPATCHER})


def add_parser(top) -> None:
    p = top.add_parser("launcher", help="install or inspect the stable launcher")
    sub = p.add_subparsers(dest="launcher_command", required=True)

    q = sub.add_parser("install", help="write $NELIX_HOME/bin/nelix atomically")
    q.add_argument("--home", default=None, help="NELIX_HOME to install into (default: resolved)")
    q.set_defaults(func=cmd_install)

    q = sub.add_parser("show", help="report the launcher's path and whether it is up to date")
    q.add_argument("--home", default=None)
    q.set_defaults(func=cmd_show)
