"""Advisory single-daemon lock for one nelix_root. The flock is the mutual-exclusion
mechanism; the JSON body is descriptive metadata so nelix-doctor can name the holder.
The lock auto-releases when the holding process dies (no stale-lock recovery)."""
import fcntl
import json
import os

try:
    from .. import paths           # package mode (hermes_plugins.nelix.daemon.singleton)
except ImportError:
    import paths                   # top-level module mode (daemon process / tests)


def acquire(lock_path, meta: dict):
    """Take an exclusive non-blocking flock on lock_path. Returns the open fd on success
    (CALLER MUST KEEP IT for the process lifetime — closing it releases the lock), or None
    if another live process holds it."""
    paths.ensure_private_dir(os.path.dirname(lock_path) or ".")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(meta).encode())
    os.fsync(fd)
    return fd


def read_holder(lock_path):
    """Best-effort read of the holder metadata. None if missing/empty/unparseable."""
    try:
        with open(lock_path) as f:
            return json.loads(f.read() or "null")
    except (OSError, ValueError):
        return None
