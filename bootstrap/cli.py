"""nelix-bootstrap — the only piece that runs before nelix exists.

It carries three modules (runtime, paths, launcher) and nothing else: it must work on a machine with
no nelix installed, which is exactly the machine where `import daemon` would fail. Everything it
prints is one JSON object on stdout, with diagnostics on stderr, so a plugin can parse it — the same
contract the installed CLI honours.

Written for Python 3.8: this runs on whatever `python3` the machine has, not on the 3.11 it is about
to provision.
"""
import argparse
import json
import shutil
import sys

BOOTSTRAP_SCHEMA = 1

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_REJECTED = 5


def emit(payload, ok=True):
    out = dict(payload)
    out["bootstrap_schema"] = BOOTSTRAP_SCHEMA
    out["ok"] = ok
    sys.stdout.write(json.dumps(out) + "\n")


def fail(code, message, exit_class=EXIT_REJECTED):
    emit({"error": {"code": code, "message": message}}, ok=False)
    sys.stderr.write("nelix-bootstrap: " + message + "\n")
    return exit_class


def require_prerequisites():
    """`uv` and a python3 are the two things this cannot provide for itself. Absent either, say so
    with the command that fixes it — a bootstrapper that fails obscurely is worse than none."""
    if shutil.which("uv") is None:
        return ("uv_missing",
                "uv is required and was not found on PATH — install it with "
                "`curl -LsSf https://astral.sh/uv/install.sh | sh`, then re-run")
    if sys.version_info < (3, 8):
        return ("python_too_old",
                "this bootstrapper needs python3 >= 3.8; found "
                + ".".join(str(p) for p in sys.version_info[:3]))
    return None


def cmd_version(_args):
    emit({"bootstrap_schema": BOOTSTRAP_SCHEMA})
    return EXIT_OK


def build_parser():
    p = argparse.ArgumentParser(prog="nelix-bootstrap",
                                description="install the nelix core from a verified release bundle")
    p.add_argument("--version", action="store_true", help="print this bootstrapper's schema and exit")
    sub = p.add_subparsers(dest="command")
    inst = sub.add_parser("install", help="install a release bundle into $NELIX_HOME")
    inst.add_argument("--bundle", default=None, help="directory holding the release artifacts")
    inst.add_argument("--manifest-sha256", dest="manifest_sha256", default=None,
                      help="the pinned digest of release-manifest.json")
    inst.add_argument("--home", default=None, help="NELIX_HOME (default: $NELIX_HOME or ~/.nelix)")
    inst.add_argument("--base-url", dest="base_url", default=None,
                      help="override the fetch source URL (the baked-in digest still protects)")
    inst.set_defaults(func=lambda a: __import__("bootstrap.install", fromlist=["run"]).run(a))
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        return cmd_version(args)
    if args.command is None:
        parser.print_help()
        return EXIT_OK
    if args.func is None:
        return fail("not_implemented", "install lands in the next task", EXIT_USAGE)
    return args.func(args)
