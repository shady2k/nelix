"""Single source of truth for nelix's on-disk layout under HERMES_HOME.

Every nelix path is defined HERE and nowhere else. Import-safe: stdlib only plus
a lazy hermes_constants probe, no project imports — so both the in-process plugin
(registry/supervisor/__init__) and the out-of-process daemon resolve identical paths.
"""
import os
from pathlib import Path


def hermes_home() -> Path:
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def nelix_root() -> Path:
    return hermes_home() / "workspace" / "nelix"


def config_path() -> Path:
    return nelix_root() / "nelix.toml"


def state_file() -> Path:
    return nelix_root() / ".active.json"


def sessions_root() -> Path:
    return nelix_root() / "sessions"


def daemon_log(stamp: str, pid: int) -> Path:
    return nelix_root() / f"daemon-{stamp}-{pid}.log"


def daemon_latest() -> Path:
    return nelix_root() / "daemon-latest.log"


# Per-spawn files only. Two wildcards ⇒ never matches single-dash daemon-latest.log.
DAEMON_LOG_GLOB = "daemon-*-*.log"
