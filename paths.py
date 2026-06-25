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


def logs_dir() -> Path:
    return nelix_root() / "logs"


def daemon_log(stamp: str, pid: int) -> Path:
    return logs_dir() / f"daemon-{stamp}-{pid}.log"


def daemon_latest() -> Path:
    return logs_dir() / "daemon-latest.log"


# Per-spawn files only. Two wildcards ⇒ never matches single-dash daemon-latest.log.
DAEMON_LOG_GLOB = "daemon-*-*.log"


def ensure_private_dir(path) -> Path:
    """Create `path` (with parents) and tighten it and every ancestor down to nelix_root to
    0700, so no nelix-owned directory is group/world-readable — transcripts and the token
    state file can hold secrets. Idempotent, and corrects a directory created earlier under
    a looser umask. (mkdir(parents=True) makes intermediate dirs with the umask, so each
    level is chmod-ed explicitly; ancestors ABOVE nelix_root, e.g. a shared HERMES_HOME, are
    left untouched.)"""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    root = nelix_root()
    p = path
    while True:
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass
        if p == root or root not in p.parents:
            break
        p = p.parent
    return path


def private_opener(path, flags):
    """Use as `open(..., opener=private_opener)` so a freshly created file is 0600 with no
    chmod race. Existing files keep their mode, but they live in a 0700 dir so stay private."""
    return os.open(path, flags, 0o600)
