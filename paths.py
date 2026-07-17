"""Single source of truth for nelix's on-disk layout under NELIX_HOME.

Every nelix path is defined HERE and nowhere else. Import-safe: stdlib only, no project
imports — so every harness and the out-of-process daemon resolve identical paths.

This file used to open "under HERMES_HOME" and root the layout at
`hermes_home()/workspace/nelix`. That was the state-side half of a mistake whose code-side
half was fixed by the plugin extraction (4d83167): Hermes is one harness among several, and a
core that keeps its sessions inside one harness's home is the same category of wrong from the
other side. The root is now the core's own, and no harness's home appears in this file.
"""
import hashlib
import os
import stat
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


# --- router runtime location [nelix-3rm.1] ---------------------------------------------
# The router's PUBLIC socket + its one-per-NELIX_HOME lock cannot live under nelix_root():
# $NELIX_HOME is operator-settable (see sun_path_overflow above), so a socket placed under it
# inherits whatever depth the operator chose — a temp dir or a worktree easily overflows
# sun_path. The router needs a location whose LENGTH never depends on $NELIX_HOME, so it is
# keyed by a fixed-width HASH of the canonical root instead of the root's own text.

# Deliberately NOT $TMPDIR: macOS sets $TMPDIR to a long per-user path
# (/var/folders/xx/.../T/), which can consume most of the sun_path budget before the per-uid/
# hash suffix is even added. "/tmp" is the shortest base every relevant platform provides;
# NELIX_RUNTIME_BASE exists so containers/tests can redirect it, not so production changes it.
DEFAULT_ROUTER_RUNTIME_BASE = "/tmp"

# sha256 hex digits used to key the runtime dir off nelix_root(). 12 hex chars = 48 bits of
# entropy — negligible collision risk across the handful of $NELIX_HOMEs one uid ever runs,
# while keeping the total socket path comfortably inside sun_path. (Same width, same
# rationale, as runtime.py's _BUILD_ID_HASH_LEN.)
ROUTER_HASH_LEN = 12


def router_runtime_base() -> Path:
    """The base the router's per-uid runtime namespace hangs off, exactly as configured:
    `NELIX_RUNTIME_BASE` (documented above), or the default "/tmp". Returned UNRESOLVED — this
    accessor has no filesystem side effects, so it does not need to touch the disk to answer.

    router_runtime_dir() resolves this (Path.resolve()) before building anything under it,
    and ensure_router_runtime_dir() is what verifies the RESOLVED target is actually safe to
    use (see RouterRuntimeBaseRejected) before anything is created there — a relative base, or
    an existing base that is group/world-writable without the sticky bit, is refused rather
    than silently trusted.
    """
    val = os.environ.get("NELIX_RUNTIME_BASE", "").strip() or DEFAULT_ROUTER_RUNTIME_BASE
    return Path(val)


def router_runtime_dir() -> Path:
    """Short, per-uid, hash-keyed runtime directory for the router's public socket + lock.

    `<resolved base>/nelix-<uid>/<hash>`: the base is resolved (`Path.resolve()`, which does
    not require it to exist — this stays a pure accessor) so the per-uid dir is always
    addressed by the base's REAL location, not whatever a symlink currently points at — a
    later swap of a symlinked base cannot silently redirect a caller away from the location
    ensure_router_runtime_dir() already verified and created. Per-uid so two users never share
    a node; the hash is over `str(nelix_root())`, which is already canonicalised (see
    nelix_root's docstring), so distinct $NELIX_HOMEs get distinct locations and any alias of
    the SAME home (symlink, `~` vs absolute, a trailing `..`) always resolves to the SAME one.

    This does not itself verify the base is SAFE to use (relative, or non-sticky
    group/world-writable bases are both hazards) — see ensure_router_runtime_dir(), the only
    sanctioned way to actually create or open anything at the location this names.
    """
    key = hashlib.sha256(str(nelix_root()).encode()).hexdigest()[:ROUTER_HASH_LEN]
    return router_runtime_base().resolve() / f"nelix-{os.getuid()}" / key


def router_sock() -> Path:
    """AF_UNIX socket node for the router's PUBLIC transport. A pure accessor like rpc_sock():
    it does not check sun_path (see sun_path_overflow — the bind site checks that) and does
    not create anything (see ensure_router_runtime_dir for that).

    Forward note for 3c (the router process): call ensure_router_runtime_dir() before binding
    here, and bind symlink-safely (e.g. verify via lstat/O_NOFOLLOW immediately before the
    bind) — a plain bind(2) follows a symlink planted after verification."""
    return router_runtime_dir() / "router.sock"


def router_lock() -> Path:
    """The router's one-per-NELIX_HOME advisory lock (daemon/singleton.py acquires it), living
    beside router_sock() in the same verified runtime dir.

    Forward note for 3c: call ensure_router_runtime_dir() before
    `daemon/singleton.py:acquire(router_lock(), ...)` — acquire() does no ownership/symlink
    verification of its own, and opens the path with a plain `os.open` — so open it (or have
    acquire open it) with O_NOFOLLOW so a symlink planted after verification is refused rather
    than followed."""
    return router_runtime_dir() / "router.lock"


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


class RouterRuntimeDirRejected(ValueError):
    """Raised by ensure_owned_private_dir() when an existing node fails the not-a-symlink /
    owner / mode check. There is no recovery path other than the operator clearing the node
    themselves — see the function's docstring for why this must never auto-correct."""


def ensure_owned_private_dir(path) -> Path:
    """Create ONE directory level as a private (0700), uid-owned, real (non-symlink)
    directory — or verify that an existing one already is all three. This is the ssh-agent /
    tmux socket-dir pattern, needed because the router's runtime dir hangs off a
    WORLD-WRITABLE parent (`/tmp` by default): a plain `mkdir` there is a pre-creation /
    symlink attack surface (an attacker who wins the race to create the node first controls
    it before we ever touch it), and ensure_private_dir doesn't help — it only tightens mode
    on a directory it already trusts, and never checks ownership or rejects a symlink.

    - If `path` does not exist: create it with mode 0700, THEN chmod it 0700 explicitly.
      Passing 0700 to mkdir is not enough by itself: mkdir's mode argument is masked by the
      process's ambient umask, so under a pathological umask that strips owner bits (e.g.
      0o700, or 0o777), a plain `os.mkdir(path, 0o700)` would transiently create the
      directory at a looser mode — 0000, in the 0o700 case — and ONLY the follow-up chmod
      would fix it, leaving a real window where a concurrent same-uid verifier could lstat
      the directory mid-creation and wrongly reject it for the wrong mode. To close that
      window we force umask 0o077 (which never strips owner bits, whatever the ambient value
      was) around the mkdir call itself, so the single mkdir(2) syscall that creates the
      directory already lands on exactly 0700 — no window, because there is no mode the
      directory is ever observed at other than 0700. The explicit chmod afterward is then
      genuine belt-and-braces, not the thing actually doing the work.
    - If `path` exists: verify via `os.lstat` — NOT `stat`, which follows a symlink and would
      report the TARGET's attributes instead of the node's own — that it is a real directory
      (not a symlink, not a file), owned by `os.getuid()`, and exactly mode 0700. Any mismatch
      RAISES RouterRuntimeDirRejected; nothing here ever chmods, chowns, or deletes an
      existing node to make it match. Adopting a node we don't already own/control is exactly
      the attack this defends against, and silently deleting something under /tmp that some
      other process created is its own hazard — surfacing the conflict is the only safe move.
    - Race-safe: `mkdir` is atomic, so a concurrent creator racing us is not a bug — its
      `FileExistsError` falls through to the same verify path a pre-existing node would take.

    Callers preparing a multi-level path (see ensure_router_runtime_dir) must call this once
    per level, SHALLOWEST FIRST, so each level is verified before the next is created inside
    it — calling it only on the deepest path would let an attacker plant a bad intermediate
    directory unnoticed.
    """
    path = Path(path)
    old_umask = os.umask(0o077)    # 0o077 never strips owner bits, whatever umask was active
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    else:
        os.chmod(path, 0o700)
        return path
    finally:
        os.umask(old_umask)

    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise RouterRuntimeDirRejected(
            f"{path} is a symlink, not a private directory; refusing to reuse or replace it")
    if not stat.S_ISDIR(st.st_mode):
        raise RouterRuntimeDirRejected(
            f"{path} exists and is not a directory; refusing to reuse it")
    if st.st_uid != os.getuid():
        raise RouterRuntimeDirRejected(
            f"{path} exists but is owned by uid {st.st_uid}, not this process's uid "
            f"{os.getuid()} (owner mismatch); refusing to adopt a node we do not own")
    if st.st_mode & 0o777 != 0o700:
        raise RouterRuntimeDirRejected(
            f"{path} exists with mode {oct(st.st_mode & 0o777)}, not 0700; refusing to "
            f"adopt it — fix its mode (or remove it, if it is not nelix's) and retry")
    return path


class RouterRuntimeBaseRejected(ValueError):
    """Raised by ensure_router_runtime_dir() when NELIX_RUNTIME_BASE itself cannot safely
    host the router's per-uid runtime tree — see verify_router_runtime_base() for the checks.
    Unlike RouterRuntimeDirRejected (an existing node under a good base failing its own check),
    this is about the base's own location or permissions being unsafe before anything is even
    created under it."""


def verify_router_runtime_base() -> Path:
    """Verify NELIX_RUNTIME_BASE (or the "/tmp" default) is safe to build the router's per-uid
    runtime tree under, and return its RESOLVED path (see router_runtime_dir() — callers must
    build under this same resolved value, never the raw configured one).

    ensure_owned_private_dir() verifies the per-uid dir and its hash child, but neither of
    those checks reaches the BASE itself. Left unchecked, a relative NELIX_RUNTIME_BASE
    resolves ambiguously against the process's cwd, and a non-default base that is
    group/world-writable WITHOUT the sticky bit lets a co-resident attacker rename/replace an
    already-verified per-uid dir out from under us — deleting or renaming a node only needs
    write permission on its PARENT, which the sticky bit is exactly what denies to everyone but
    the file's owner (and root). The result of skipping this check would not be a mere
    pre-creation race like the base's own docstring above describes: it would be the router's
    socket binding, or its lock opening, inside attacker-controlled territory.

    - A RELATIVE base is rejected outright: it names no fixed location, so "safe" cannot even
      be evaluated.
    - The base is then RESOLVED (`Path.resolve()`) before any check — the default "/tmp" is
      itself a symlink to "/private/tmp" on macOS, and the resolved target, not the symlink
      spelling, is what the filesystem actually enforces permissions on.
    - The resolved base must exist and be a directory, and if it is group- or world-writable
      (`st_mode & (S_IWGRP | S_IWOTH)`), it MUST also carry the sticky bit (`S_ISVTX`) — exactly
      how the real `/tmp` is configured (mode 1777). A root-owned or otherwise private
      (not group/world-writable) base needs no sticky bit: nobody but its owner can rename
      inside it regardless.

    Any failure raises RouterRuntimeBaseRejected; nothing here creates, chmods, or otherwise
    touches the base — an operator-supplied location that fails this check must be fixed (or
    replaced) by the operator, the same "surface the conflict, never auto-correct" stance
    ensure_owned_private_dir takes for the per-uid dir itself.
    """
    base = router_runtime_base()
    if not base.is_absolute():
        raise RouterRuntimeBaseRejected(
            f"NELIX_RUNTIME_BASE={str(base)!r} is a relative path; refusing an ambiguous, "
            f"cwd-dependent runtime base — set it to an absolute path")
    resolved = base.resolve()
    try:
        st = os.stat(resolved)
    except OSError as e:
        raise RouterRuntimeBaseRejected(
            f"NELIX_RUNTIME_BASE {str(base)!r} (resolved: {str(resolved)!r}) is not usable: "
            f"{e}") from e
    if not stat.S_ISDIR(st.st_mode):
        raise RouterRuntimeBaseRejected(
            f"NELIX_RUNTIME_BASE {str(base)!r} (resolved: {str(resolved)!r}) is not a "
            f"directory; refusing to use it")
    if (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)) and not (st.st_mode & stat.S_ISVTX):
        raise RouterRuntimeBaseRejected(
            f"NELIX_RUNTIME_BASE {str(base)!r} (resolved: {str(resolved)!r}) is mode "
            f"{oct(st.st_mode & 0o7777)}: group/world-writable WITHOUT the sticky bit. A "
            f"co-resident user could rename or replace our runtime dir out from under us — "
            f"refusing to use it. Either `chmod +t` it (like the real /tmp, mode 1777), or "
            f"point NELIX_RUNTIME_BASE at a location only this uid (or root) can write to")
    return resolved


def ensure_router_runtime_dir() -> Path:
    """Create-or-verify the router's full runtime location, shallowest level first: the
    per-uid base directory, then the hash subdir inside it. This is the ONLY sanctioned way
    to prepare the directory router_sock()/router_lock() live in — see
    ensure_owned_private_dir for why a plain mkdir(parents=True) is not safe here, and
    verify_router_runtime_base() for why the BASE itself cannot be trusted unexamined before
    that. Returns router_runtime_dir().

    Forward note for 3c (the router process): call this BEFORE binding router_sock() or
    calling `daemon/singleton.py:acquire(router_lock(), ...)` — acquire() performs no
    ownership/symlink verification of its own — and open/bind both symlink-safely (e.g.
    O_NOFOLLOW) so a symlink planted after this check is refused rather than followed.
    """
    verify_router_runtime_base()
    d = router_runtime_dir()
    for level in (d.parent, d):
        ensure_owned_private_dir(level)
    return d
