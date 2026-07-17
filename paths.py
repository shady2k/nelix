"""Single source of truth for nelix's on-disk layout under NELIX_HOME.

Every nelix path is defined HERE and nowhere else. Import-safe: stdlib only, no project
imports — so every harness and the out-of-process daemon resolve identical paths.

This file used to open "under HERMES_HOME" and root the layout at
`hermes_home()/workspace/nelix`. That was the state-side half of a mistake whose code-side
half was fixed by the plugin extraction (4d83167): Hermes is one harness among several, and a
core that keeps its sessions inside one harness's home is the same category of wrong from the
other side. The root is now the core's own, and no harness's home appears in this file.
"""
import os
from pathlib import Path

# The default state root. NOT XDG: macOS normally has no XDG_RUNTIME_DIR, so an XDG default
# would resolve to a different place on the two platforms we care about — or to nothing.
DEFAULT_NELIX_HOME = "~/.nelix"

# macOS `sockaddr_un.sun_path` is 104 bytes INCLUDING the NUL terminator (Linux allows 108).
# We check against the SMALLER: a socket path that binds on Linux but not on macOS is a
# portability trap that only shows up on the other machine. Measured on darwin 24.6.0:
# bind() succeeds at 103 bytes and raises OSError("AF_UNIX path too long") at 104.
SUN_PATH_MAX = 104


def nelix_root() -> Path:
    """The core's private state root: `$NELIX_HOME`, else `~/.nelix`. This IS $NELIX_HOME —
    there is no deeper nesting; `runtimes/`, `sessions/` and the rest hang directly off it.

    CANONICAL, and canonicalised HERE so it cannot be forgotten downstream: `~/.nelix`,
    `/Users/x/.nelix` and any symlink alias all name ONE root. That matters because root
    identity is daemon identity — `daemon.lock` and `rpc.sock` live under this path, so two
    spellings of one directory must not read as two daemons. Note the filesystem already
    canonicalises for the LOCK (aliases reach one inode, and flock is per-inode), so this is
    belt-and-braces today; it becomes load-bearing the moment anything keys off the root's
    NAME rather than its inode — which is exactly what the router's per-uid runtime location
    is specified to do [nelix-3rm].
    """
    val = os.environ.get("NELIX_HOME", "").strip() or DEFAULT_NELIX_HOME
    return Path(val).expanduser().resolve()


def config_path() -> Path:
    return nelix_root() / "nelix.toml"


def state_file() -> Path:
    return nelix_root() / ".active.json"


def rpc_sock() -> Path:
    """AF_UNIX socket node for the local RPC transport. Lives in the 0700 nelix_root (so the node
    inherits a private dir); the node itself is created 0600 by the daemon at bind time.

    A pure accessor: it does NOT check sun_path. See sun_path_overflow() for why not, and for
    who does.
    """
    return nelix_root() / "rpc.sock"


def sun_path_overflow(path) -> str | None:
    """Why `path` cannot be an AF_UNIX node on this platform, or None if it fits.

    Returns a reason instead of raising, and lives here rather than inside rpc_sock(), because
    rpc_sock() is a path accessor and an accessor that throws breaks every caller that only
    wants the string. That is measured, not hypothetical: pytest's own tmp_path on macOS is
    ~125 bytes, so a checking rpc_sock() red-lights tests that never bind a thing. The BIND
    site raises — that is where the limit is actually enforced, and it is the only place that
    knows a bind is about to happen.

    Worth guarding at all because $NELIX_HOME is operator-settable now: an unguarded bind fails
    with a bare OSError("AF_UNIX path too long") naming neither the path, the limit, nor the
    setting that caused it — and it fails AFTER server_bind() has already unlinked the node.
    """
    n = len(str(path).encode())
    if n < SUN_PATH_MAX:           # <, not <=: the limit counts the NUL terminator
        return None
    return (f"AF_UNIX socket path {str(path)!r} is {n} bytes; this platform allows at most "
            f"{SUN_PATH_MAX - 1}. If it sits under $NELIX_HOME, point NELIX_HOME at a shorter "
            f"path; if it came from $NELIX_RPC_SOCK, shorten that.")


def sessions_root() -> Path:
    return nelix_root() / "sessions"


def runtimes_root() -> Path:
    """Where installed runtimes live, one immutable directory per build id [nelix-9a4.2]."""
    return nelix_root() / "runtimes"


def runtime_dir(build_id: str) -> Path:
    """A single generation's runtime: VERSION-ADDRESSED, and never written again once its
    manifest exists. This is what makes "old sessions keep running old code" true rather than
    aspirational: daemon/broker_client.py respawns a dead broker with
    `[sys.executable, "-m", "daemon.pty_broker"]`, so if an upgrade replaced the files under a
    live generation, that respawn would import the NEW code into an OLD session. It cannot,
    because an upgrade never touches this directory — it creates a different one."""
    return runtimes_root() / build_id


def runtime_python(build_id: str) -> Path:
    """The interpreter a generation runs. Everything the daemon spawns via sys.executable —
    the PTY broker above all — inherits it, so pinning THIS pins the whole generation."""
    return runtime_dir(build_id) / "venv" / "bin" / "python"


def runtime_interpreter_home(build_id: str) -> Path:
    """The generation's OWN copy of the base interpreter (binary + stdlib), which `venv/` is
    built from and points `pyvenv.cfg: home` at.

    It is copied in rather than shared because a venv retains NOTHING by itself — measured
    2026-07-17, and this is the nelix-cb0 door reopening: `uv venv --python 3.11` symlinks
    `venv/bin/python` at `~/.local/share/uv/python/cpython-3.11-macos-aarch64-none`, an
    UNVERSIONED alias that is itself a symlink to the current patch, and `sys.base_prefix`
    (hence the whole stdlib) resolves through it. So `uv python install 3.11.16` would silently
    re-point every "immutable" runtime at a different interpreter, and `uv python uninstall`
    would break them all — while the runtime directory stayed byte-for-byte identical.
    `python -m venv --copies` does NOT fix it: it copies the 17MB binary and leaves `home` (and
    therefore `os.py`) in the shared store."""
    return runtime_dir(build_id) / "python"


def runtime_manifest(build_id: str) -> Path:
    """The install's COMMIT MARKER, written last and atomically: a runtime directory without it
    is a partial install and is never used.

    The tree cannot be staged elsewhere and renamed into place — the trick `_write_state` uses
    for files. Measured 2026-07-17: a venv records ABSOLUTE paths (`pyvenv.cfg: home`, and a
    `bin/python` symlink into the interpreter home), so a staged venv is dead on arrival after
    the rename — `bin/python` dangles. The build therefore happens AT the final path and commits
    with this one small file."""
    return runtime_dir(build_id) / "manifest.json"


def runtime_current() -> Path:
    """Mutable pointer to the generation new daemons start from. The pointer moves; the runtime
    it named does not — an upgrade repoints this symlink, and anything already running holds its
    own runtime path and never notices."""
    return runtimes_root() / "current"


def runtime_install_lock() -> Path:
    """Serializes concurrent installers building into runtimes_root()."""
    return runtimes_root() / ".install.lock"


def session_meta(session_dir) -> Path:
    """Per-session metadata sidecar (cols/rows/executor/driver). Single source of the filename so
    the daemon (writer) and the nelix-capture tool (reader) agree."""
    return Path(session_dir) / "meta.json"


def session_owner(session_dir) -> Path:
    """The session's durable owner record (daemon/owner.py). A SEPARATE file from session_meta,
    not a field in it: meta.json is a best-effort capture sidecar whose writer swallows OSError,
    and an access invariant cannot share a file with data that is allowed to go missing."""
    return Path(session_dir) / "owner.json"


def child_record(session_dir) -> Path:
    """Per-session reaping record (pid/pgid + fingerprints + owning daemon). Lives in the
    session dir so it shares the session's lifetime; never in a separate top-level folder."""
    return Path(session_dir) / "child.json"


def daemon_lock() -> Path:
    """Advisory single-daemon lock for this nelix_root (one daemon per NELIX_HOME)."""
    return nelix_root() / "daemon.lock"


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
    level is chmod-ed explicitly.)

    The walk now tightens nelix_root ITSELF and stops there. Under the old layout the root was
    a subdirectory we created (`<hermes_home>/workspace/nelix`) and the loop deliberately left
    its ancestors — a shared HERMES_HOME — alone. The root IS $NELIX_HOME now, so the dir we
    chmod 0700 is the one the operator named for us. Ancestors above it are still never
    touched: point NELIX_HOME at a directory that is nelix's, not at $HOME.
    """
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
