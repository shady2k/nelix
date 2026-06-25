"""Executor registry — reads $HERMES_HOME/workspace/nelix/nelix.toml (name -> command).

Nelix runs each executor's command verbatim; secret injection / wrappers are
entirely the operator's concern (BRD non-goal: not a sandbox).
"""
import shutil
import tomllib
from pathlib import Path

try:
    from .paths import config_path, ensure_private_dir
except ImportError:           # loaded as a top-level module (tests/standalone)
    from paths import config_path, ensure_private_dir

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
