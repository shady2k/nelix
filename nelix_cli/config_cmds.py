"""`nelix config` — the ONLY sanctioned way for a host adapter's wizard to touch
$NELIX_HOME/nelix.toml. Validation lives in daemon.config (already the single source of executor
validation); this module adds no rules of its own and proves every write by loading it back."""
import shutil

import paths
from daemon.config import load_executors
from nelix_cli.envelope import EXIT_REJECTED, emit_error, emit_ok
from nelix_cli.toml_emit import executor_table

_DEFAULT_DRIVER = "claude"
_DEFAULT_LAUNCHER = "local"


def _spec_dict(spec) -> dict:
    return {"command": spec.command, "args": list(spec.args), "driver": spec.driver,
            "launcher": spec.launcher, "env": dict(spec.env)}


def cmd_list(args) -> int:
    loaded = load_executors(paths.config_path())
    if loaded.parse_error is not None:
        return emit_error("config_unreadable", loaded.parse_error, exit_class=EXIT_REJECTED)
    return emit_ok({"config_path": str(paths.config_path()),
                    "executors": {n: _spec_dict(s) for n, s in loaded.specs.items()},
                    "errors": loaded.executor_errors})


def cmd_validate(args) -> int:
    loaded = load_executors(paths.config_path())
    if loaded.parse_error is not None:
        return emit_error("config_unreadable", loaded.parse_error, exit_class=EXIT_REJECTED)
    if loaded.executor_errors:
        return emit_error("executor_invalid", "some executors are malformed",
                          exit_class=EXIT_REJECTED, details={"errors": loaded.executor_errors})
    return emit_ok({"config_path": str(paths.config_path()),
                    "executors": sorted(loaded.specs)})


def cmd_add(args) -> int:
    path = paths.config_path()
    existing = load_executors(path)
    if args.name in existing.specs:
        return emit_error("executor_exists", f"executor {args.name!r} is already configured",
                          exit_class=EXIT_REJECTED)
    if shutil.which(args.command) is None:
        return emit_error("command_not_found",
                          f"{args.command!r} is not on PATH — install it or give a full path",
                          exit_class=EXIT_REJECTED)

    block = executor_table(args.name, {"command": args.command, "args": list(args.arg or []),
                                       "driver": args.driver, "launcher": args.launcher})
    paths.ensure_private_dir(path.parent)
    prefix = "" if not path.exists() or path.read_text(encoding="utf-8").endswith("\n\n") else "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(prefix + block)

    # Prove the write: whatever we just appended must load through the daemon's own validator,
    # otherwise the wizard would report success over a file the daemon will silently skip.
    reloaded = load_executors(path)
    if args.name not in reloaded.specs:
        problem = next((e for e in reloaded.executor_errors if e.get("name") == args.name), None)
        return emit_error("executor_invalid",
                          f"wrote {args.name!r} but the loader rejected it",
                          exit_class=EXIT_REJECTED, details={"problem": problem})
    return emit_ok({"config_path": str(path), "added": args.name,
                    "executor": _spec_dict(reloaded.specs[args.name])})


def add_parser(top) -> None:
    cfg = top.add_parser("config", help="inspect and extend the executor registry")
    sub = cfg.add_subparsers(dest="config_command", required=True)

    p = sub.add_parser("list", help="list configured executors")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("validate", help="check the config loads and every executor is well-formed")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("add", help="append one executor, then prove it loads")
    p.add_argument("--name", required=True)
    p.add_argument("--command", required=True)
    p.add_argument("--arg", action="append", default=[],
                   help="repeatable: one argv entry for the executor command; use --arg=-x for values that start with a dash")
    p.add_argument("--driver", default=_DEFAULT_DRIVER)
    p.add_argument("--launcher", default=_DEFAULT_LAUNCHER)
    p.set_defaults(func=cmd_add)
