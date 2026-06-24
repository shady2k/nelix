"""Executor registry — reads $HERMES_HOME/nelix/nelix.toml (name -> command).

Nelix runs each executor's command verbatim; secret injection / wrappers are
entirely the operator's concern (BRD non-goal: not a sandbox).
"""
import os
import shutil
import tomllib
from pathlib import Path

_EXAMPLE = Path(__file__).parent / "nelix.toml.example"


def hermes_home() -> Path:
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def config_path() -> Path:
    return hermes_home() / "nelix" / "nelix.toml"


def names() -> list:
    try:
        with open(config_path(), "rb") as f:
            return sorted(tomllib.load(f).get("executors", {}))
    except Exception:
        return []


def seed_if_absent() -> bool:
    dest = config_path()
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_EXAMPLE, dest)
    return True
