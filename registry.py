"""Executor registry — reads $HERMES_HOME/workspace/nelix/nelix.toml (name -> command).

Nelix runs each executor's command verbatim; secret injection / wrappers are
entirely the operator's concern (BRD non-goal: not a sandbox).
"""
import shutil
import tomllib
from pathlib import Path

try:
    from .paths import config_path, ensure_private_dir
    from .daemon.config import load_executors
except ImportError:           # loaded as a top-level module (tests/standalone)
    from paths import config_path, ensure_private_dir
    from daemon.config import load_executors

_EXAMPLE = Path(__file__).parent / "nelix.toml.example"


def names() -> list:
    try:
        with open(config_path(), "rb") as f:
            return sorted(tomllib.load(f).get("executors", {}))
    except Exception:
        return []


def seed_if_absent() -> bool:
    dest = config_path()
    ensure_private_dir(dest.parent)              # tighten the nelix dir even if the config already exists
    if dest.exists():
        return False
    shutil.copy2(_EXAMPLE, dest)
    return True


def validate() -> dict:
    """In-process config validation for nelix_start. Reads the same nelix.toml the daemon
    loads (single source: daemon.config.load_executors) and returns just the error view."""
    load = load_executors(config_path())
    return {"parse_error": load.parse_error, "executor_errors": load.executor_errors}


def config_error_for(validation, executor):
    """Structured, relayable config error for a requested executor, or None.

    - whole-file parse error  -> the file is broken; nothing can start.
    - executor present-but-disabled (in executor_errors) -> name it as a config problem.
    - valid, or simply absent (a typo) -> None: let the daemon return its 'unknown executor'.
    """
    pe = validation.get("parse_error")
    if pe:
        return {"error": f"nelix config error: cannot load nelix.toml — {pe}",
                "config_errors": [{"name": None, "problem": pe}]}
    for e in validation.get("executor_errors", []):
        if e["name"] == executor:
            return {"error": (f"executor {executor!r} is unavailable due to a config error: "
                              f"{e['problem']}"),
                    "config_errors": validation["executor_errors"]}
    return None
