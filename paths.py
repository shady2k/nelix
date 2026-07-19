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
import threading
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

# Filenames inside the verified runtime dir. Named once here so router_sock()/router_lock()
# and resolved_router_paths() cannot drift apart on what the two nodes are called.
ROUTER_SOCK_NAME = "router.sock"
ROUTER_LOCK_NAME = "router.lock"


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


def _router_runtime_dir_from(resolved_base: Path) -> Path:
    """`<resolved_base>/nelix-<uid>/<hash>`, given a base that is ALREADY resolved. The one
    place the per-uid, hash-keyed shape is built, shared by router_runtime_dir() (which
    resolves the configured base itself — fine for a pure accessor with no security load) and
    resolved_router_paths() (which resolves the base exactly ONCE and passes that single
    result here — see resolved_router_paths()'s docstring for why re-resolving is a TOCTOU).

    Per-uid so two users never share a node; the hash is over `str(nelix_root())`, which is
    already canonicalised (see nelix_root's docstring), so distinct $NELIX_HOMEs get distinct
    locations and any alias of the SAME home (symlink, `~` vs absolute, a trailing `..`)
    always resolves to the SAME one.
    """
    key = hashlib.sha256(str(nelix_root()).encode()).hexdigest()[:ROUTER_HASH_LEN]
    return resolved_base / f"nelix-{os.getuid()}" / key


def router_runtime_dir() -> Path:
    """Short, per-uid, hash-keyed runtime directory for the router's public socket + lock.

    A pure accessor: it resolves the CONFIGURED base itself (`Path.resolve()`, which does not
    require it to exist), so the per-uid dir is always addressed by the base's REAL location,
    not whatever a symlink currently points at right now. That is enough for callers that
    just want a string (tests, logging) but is NOT enough for a security-sensitive caller
    preparing to verify-then-build the directory: this function re-resolves independently of
    verify_router_runtime_base(), so calling both is TWO resolutions of a base that could
    change in between. See resolved_router_paths() for the single-resolution alternative
    ensure_router_runtime_dir() (and 3c) must use instead.

    This does not itself verify the base is SAFE to use (relative, or non-sticky
    group/world-writable bases are both hazards) — see ensure_router_runtime_dir(), the only
    sanctioned way to actually create or open anything at the location this names.
    """
    return _router_runtime_dir_from(router_runtime_base().resolve())


def router_sock() -> Path:
    """AF_UNIX socket node for the router's PUBLIC transport. A pure accessor like rpc_sock():
    it does not check sun_path (see sun_path_overflow — the bind site checks that) and does
    not create anything (see ensure_router_runtime_dir for that).

    Forward note for 3c (the router process): do NOT combine this accessor with a separate
    ensure_router_runtime_dir() call to prepare the directory — each independently resolves
    NELIX_RUNTIME_BASE, and mixing re-resolutions is the TOCTOU resolved_router_paths() exists
    to close. Get `(dir, sock, lock)` from resolved_router_paths() in ONE call instead, verify/
    create the dir from THAT `dir` (ensure_owned_private_dir, shallowest first), and bind
    symlink-safely (e.g. O_NOFOLLOW immediately before the bind) — a plain bind(2) follows a
    symlink planted after verification."""
    return router_runtime_dir() / ROUTER_SOCK_NAME


def router_lock() -> Path:
    """The router's one-per-NELIX_HOME advisory lock (daemon/singleton.py acquires it), living
    beside router_sock() in the same verified runtime dir.

    Forward note for 3c: as with router_sock(), do NOT pair this with a separate
    ensure_router_runtime_dir() call — use resolved_router_paths() once and take `lock` from
    it. `daemon/singleton.py:acquire(router_lock(), ...)` does no ownership/symlink
    verification of its own, and opens the path with a plain `os.open` — so open it (or have
    acquire open it) with O_NOFOLLOW so a symlink planted after verification is refused rather
    than followed."""
    return router_runtime_dir() / ROUTER_LOCK_NAME


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

# Per-generation log glob (only daemon spawn logs, never latest-pointers or other files).
GENERATION_LOG_GLOB = "gen-*-*-*.log"


def _validate_generation_id(generation_id: str) -> None:
    """Validate generation_id shape BEFORE building any path component from it. A ``..`` or
    ``/`` in an unvalidated id is a path-traversal attack. Imported lazily so paths.py stays
    import-safe (stdlib only, no project imports) — this function is only called from
    functions that already do a project import.
    """
    from nelix_contracts.ids import validate_generation_id as _validate
    _validate(generation_id)


def generations_root() -> Path:
    """Root of all per-generation state directories: ``nelix_root()/generations``."""
    return nelix_root() / "generations"


def generation_dir(generation_id: str) -> Path:
    """A single generation's state directory: ``generations_root()/<generation-id>``.

    The entire path, including the id, is validated: ``ensure_owned_private_dir()`` (the
    stronger owned + non-symlink check) must be applied at the generations_root() level
    first, then at this level — see the per-generation supervisor for that pattern.
    """
    _validate_generation_id(generation_id)
    return generations_root() / generation_id


def generation_lock(generation_id: str) -> Path:
    """The advisory flock file inside a generation's state directory."""
    return generation_dir(generation_id) / "daemon.lock"


def generation_state(generation_id: str) -> Path:
    """The per-generation .active.json equivalent inside a generation's state directory."""
    return generation_dir(generation_id) / ".active.json"


# The socket node name inside the per-generation runtime dir. Short and fixed-width so the
# total path stays comfortably inside sun_path regardless of the generation id length.
GENERATION_SOCK_NAME = "gen.sock"


def generation_runtime_base() -> Path:
    """Short base for per-generation socket directories, shared with the router's runtime
    base — reuses ``NELIX_RUNTIME_BASE`` (same env var, same default ``/tmp``) so there is
    exactly ONE place the operator configures a short path, not two separate env vars that
    could drift. This is NOT a code-sharing cost: ``ensure_owned_private_dir`` (the stronger
    owned + non-symlink dir check) and ``sun_path_overflow`` already exist for the router's
    scheme and the generation supervisor uses the same functions and same base.
    """
    return router_runtime_base()


def _generation_runtime_dir_from(resolved_base: Path, generation_id: str) -> Path:
    """``<resolved_base>/nelix-<uid>/gen-<hash>/<generation_id>`` where the hash is over
    ``str(nelix_root()) + generation_id`` — the root-hash prevents cross-root alias confusion
    (same property as the router's ``_router_runtime_dir_from``), and appending the
    generation id itself names the generation's socket directory so a concurrent supervisor
    for a different generation id writes to a separate directory.
    """
    key = hashlib.sha256((str(nelix_root()) + generation_id).encode()).hexdigest()[:ROUTER_HASH_LEN]
    return resolved_base / f"nelix-{os.getuid()}" / f"gen-{key}" / generation_id


def generation_runtime_dir(generation_id: str) -> Path:
    """Short, per-uid, per-generation runtime directory for the generation's AF_UNIX socket.
    Follows the same hash-keyed /tmp namespace pattern as the router's runtime dir, so the
    total socket path is bounded by the fixed-width components regardless of NELIX_HOME depth.

    The caller must verify-then-build this directory with ``ensure_owned_private_dir()`` at
    each level (parent first, then the dir), same as ``ensure_router_runtime_dir()`` does for
    the router's runtime dir.
    """
    _validate_generation_id(generation_id)
    return _generation_runtime_dir_from(generation_runtime_base().resolve(), generation_id)


def generation_sock(generation_id: str) -> Path:
    """AF_UNIX socket node for a per-generation daemon. Lives in the short runtime dir
    (same scheme as ``router_sock()``) so the socket path always fits inside
    ``sun_path`` regardless of NELIX_HOME.
    """
    _validate_generation_id(generation_id)
    return generation_runtime_dir(generation_id) / GENERATION_SOCK_NAME


# --- per-generation logs ----------------------------------------------------
# A generation daemon writes ``gen-<generation_id>-<stamp>-<pid>.log`` in the same
# logs directory as the uid-wide daemon. The generation_id component disambiguates
# concurrent spawns from two generations in the same clock tick.


def generation_log(generation_id: str, stamp: str, pid: int) -> Path:
    """A single per-generation daemon's log file."""
    _validate_generation_id(generation_id)
    return logs_dir() / f"gen-{generation_id}-{stamp}-{pid}.log"


def generation_latest(generation_id: str) -> Path:
    """Symlink to the latest per-generation log."""
    _validate_generation_id(generation_id)
    return logs_dir() / f"gen-{generation_id}-latest.log"


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


# os.umask is PROCESS-GLOBAL, not per-thread: two concurrent same-process creators each
# racing through the umask(0o077) -> mkdir -> umask(restore) window in ensure_owned_private_dir
# could interleave without something serializing them, leaving the ambient umask at 0o077 (or
# some other wrong value) for whatever OTHER code in this process runs mid-window — a
# same-process hazard entirely distinct from the cross-process attacker ensure_owned_private_dir
# otherwise defends against. The window is kept tiny (just the umask/mkdir/umask itself, not
# the lstat verification that follows) so contention stays negligible.
_UMASK_LOCK = threading.Lock()


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
    with _UMASK_LOCK:                  # serialize the process-global umask window (see above)
        old_umask = os.umask(0o077)    # 0o077 never strips owner bits, whatever umask was active
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            created = False
        else:
            os.chmod(path, 0o700)
            created = True
        finally:
            os.umask(old_umask)
    if created:
        return path

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
    """Raised by ensure_router_runtime_dir() when NELIX_RUNTIME_BASE itself — or one of its
    ANCESTOR directories — cannot safely host the router's per-uid runtime tree — see
    verify_router_runtime_base() for the checks. Unlike RouterRuntimeDirRejected (an existing
    node under a good base failing its own check), this is about the base's location or
    permissions (or one of the directories above it) being unsafe before anything is even
    created under it."""


def _verify_component_trust(path: Path, label: str) -> None:
    """One component of verify_router_runtime_base()'s ancestry walk — the resolved base
    itself, or one of its ancestors up to "/" — checked against the single trust rule that
    walk enforces at every level: a real directory, owned by root or by this process's own
    uid, and if it is group- or world-writable, carrying the sticky bit.

    Folded out of verify_router_runtime_base() so the base's own check and every ancestor's
    check are ONE piece of logic, not two copies that could drift apart. `label` is a
    caller-supplied description of `path` (naming it as the base or as "ancestor of ...") so
    the raised message always identifies exactly which component in the chain failed.

    A failed `os.stat` — the component does not exist, or is not reachable — is a REJECTION,
    not a skip: an ancestor we cannot even stat is not one we can vouch for, and silently
    treating "unknown" as "trusted" is exactly the fail-open mistake this whole check exists
    to avoid.
    """
    try:
        st = os.stat(path)
    except OSError as e:
        raise RouterRuntimeBaseRejected(f"{label} {str(path)!r} is not usable: {e}") from e
    if not stat.S_ISDIR(st.st_mode):
        raise RouterRuntimeBaseRejected(
            f"{label} {str(path)!r} is not a directory; refusing to use it")
    if st.st_uid not in (0, os.getuid()):
        raise RouterRuntimeBaseRejected(
            f"{label} {str(path)!r} is owned by uid {st.st_uid}, neither root nor this "
            f"process's own uid {os.getuid()} — its owner could rename or replace our "
            f"runtime dir (or the base itself, if this is an ancestor of it) out from under "
            f"us regardless of mode (the sticky bit stops other users, never the node's own "
            f"owner); refusing to use it. Point NELIX_RUNTIME_BASE at a location whose full "
            f"ancestry is owned by root or by this uid")
    if (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)) and not (st.st_mode & stat.S_ISVTX):
        raise RouterRuntimeBaseRejected(
            f"{label} {str(path)!r} is mode {oct(st.st_mode & 0o7777)}: group/world-writable "
            f"WITHOUT the sticky bit. A co-resident user could rename or replace it (and "
            f"anything built under it, including our already-verified runtime dir) out from "
            f"under us — refusing to use it. Either `chmod +t` it (like the real /tmp, mode "
            f"1777), or point NELIX_RUNTIME_BASE at a location whose full ancestry only this "
            f"uid (or root) can write to")


def verify_router_runtime_base() -> Path:
    """Verify NELIX_RUNTIME_BASE (or the "/tmp" default) — and every directory ABOVE it, up to
    "/" — is safe to build the router's per-uid runtime tree under, and return the base's
    RESOLVED path. A security-sensitive caller must build under THIS SAME resolved value,
    never the raw configured one and never a SECOND, fresh resolution — see
    resolved_router_paths(), which calls this function and reuses its return value rather than
    resolving the base again.

    ensure_owned_private_dir() verifies the per-uid dir and its hash child, but neither of
    those checks reaches the BASE itself, and checking the base alone is not enough either: a
    base that is itself victim-owned and mode-safe can still be pulled out from under us if
    some ANCESTOR of it is attacker-controlled. Renaming or replacing a directory only needs
    write permission on its PARENT — so an attacker who owns (or can write to, sans sticky bit)
    any directory ABOVE the base can swap the base out after we have already verified it, and
    our subsequent pathname-based mkdir lands under the replacement instead. Leaf-only checks
    do not see this: they stat the base's own node and stop, never looking at what holds it.

    - A RELATIVE base is rejected outright: it names no fixed location, so "safe" cannot even
      be evaluated.
    - The base is then RESOLVED (`Path.resolve()`) before any check — the default "/tmp" is
      itself a symlink to "/private/tmp" on macOS, and the resolved target, not the symlink
      spelling, is what the filesystem actually enforces permissions on.
    - Every component from the resolved base up to "/" — the base itself, then each of
      `resolved.parents` — is checked with the SAME rule (see _verify_component_trust): it
      must exist and be a real directory; it must be OWNED BY root (uid 0) or by this
      process's own uid (`st_uid in (0, os.getuid())`); and if it is group- or world-writable
      (`st_mode & (S_IWGRP | S_IWOTH)`), it MUST also carry the sticky bit (`S_ISVTX`) —
      exactly how the real `/tmp` is configured (mode 1777). A root-owned or otherwise
      private (not group/world-writable) directory needs no sticky bit: nobody but its owner
      can rename inside it regardless.
    - The ownership rule is checked IN ADDITION to the mode/sticky rule, not instead of it, at
      every level: the sticky bit only denies rename/delete rights to everyone EXCEPT the
      node's OWNER (and root) — it stops a co-resident stranger, but does nothing at all
      against the node's own owner. An attacker-owned `1777` directory therefore clears the
      mode/sticky check yet its owner can still rename our already-verified tree out from
      under us — and an attacker-owned directory that merely LOOKS private (not
      group/world-writable, so the sticky branch never even triggers) is exactly as
      attacker-controlled, just via a different-looking mode. Root is exempted because root
      can already do anything to our files regardless of this check; our own uid is exempted
      because a directory we own cannot be used against us by definition.
    - Any component failing either rule raises RouterRuntimeBaseRejected NAMING that
      component, not just the base — including a component `os.stat` cannot even reach
      (failing closed rather than treating an unreadable ancestor as trusted).

    IMPORTANT — what this check is, and is not: this is a pathname-based FAIL-FAST pre-check.
    It closes misconfiguration (a relative or missing base) and the common attacker cases (a
    hostile or hostile-ancestor base) loudly and early, before any syscall touches the
    filesystem. But pathname checks are inherently racy against an attacker who controls ANY
    directory in the path: nothing stops that directory's owner from replacing a component
    the instant after this function returns and before the caller's next syscall runs — this
    walk narrows the window and the set of attackers who can win the race, it does not close
    it. The ATOMIC security boundary is the ROUTER PROCESS itself (slice 3c): it MUST
    establish and hold the runtime-dir hierarchy via FD-RELATIVE traversal — `os.open(...,
    O_DIRECTORY | O_NOFOLLOW)` from a trusted anchor, then `mkdir`/`stat`/`bind`/lock-open
    relative to the held directory FDs (e.g. via `dir_fd=`) — never by re-resolving pathnames
    once it has started building. That fd-relative construction is the residual this function
    explicitly leaves to 3c; this function's job ends at "loudly refuse the cases a pathname
    check CAN see."

    Nothing here creates, chmods, or otherwise touches the base or any ancestor; an
    operator-supplied location that fails this check must be fixed (or replaced) by the
    operator, the same "surface the conflict, never auto-correct" stance ensure_owned_private_dir
    takes for the per-uid dir itself.
    """
    base = router_runtime_base()
    if not base.is_absolute():
        raise RouterRuntimeBaseRejected(
            f"NELIX_RUNTIME_BASE={str(base)!r} is a relative path; refusing an ambiguous, "
            f"cwd-dependent runtime base — set it to an absolute path")
    resolved = base.resolve()
    _verify_component_trust(
        resolved, f"NELIX_RUNTIME_BASE {str(base)!r} (resolved: {str(resolved)!r})")
    for ancestor in resolved.parents:
        _verify_component_trust(
            ancestor,
            f"NELIX_RUNTIME_BASE {str(base)!r} (resolved: {str(resolved)!r}) ancestor")
    return resolved


def resolved_router_paths() -> tuple[Path, Path, Path]:
    """Resolve NELIX_RUNTIME_BASE EXACTLY ONCE and derive `(runtime_dir, sock, lock)` from
    that single resolution. This is the single-resolution counterpart to calling
    verify_router_runtime_base(), router_sock(), and router_lock() (or router_runtime_dir())
    separately — each of those, on its own, calls `Path.resolve()` on the configured base
    independently. Chaining independent re-resolutions is a TOCTOU: a base that is a symlink
    can answer safely the first time (verify_router_runtime_base() checks it) and point
    somewhere else — attacker-controlled — the second time a fresh resolve() runs to build
    the directory the socket/lock actually live under, because the base can be repointed in
    the window between the two calls. Resolving once here and deriving all three paths from
    that one value closes the window between VERIFYING the base and CONSTRUCTING the location.

    Does not create or touch anything on disk beyond what verify_router_runtime_base() itself
    does (nothing) — this is still a verify-and-compute step, not a build step. A caller that
    needs the directory to actually exist calls ensure_owned_private_dir() on the returned
    dir's parent then the dir itself (shallowest first, exactly as ensure_router_runtime_dir()
    does internally) using the `dir` THIS function returned — never by calling
    router_runtime_dir() again, which would re-resolve.

    Forward note for 3c (the router process): this is the entry point to use, once, to get
    everything needed to prepare and bind/lock the router's runtime location. Do not ALSO call
    ensure_router_runtime_dir() alongside it — that performs its own independent call to this
    same function (and therefore its own independent resolution); pick one call site. Either
    call this once and do the ensure_owned_private_dir loop yourself, or call
    ensure_router_runtime_dir() alone when you only need the directory back (it returns the
    same `dir` this function computes). Whichever is used, still bind/open `sock`/`lock`
    symlink-safely (O_NOFOLLOW) at the actual syscall — resolving once here defends against
    the base being swapped BETWEEN verify and construct, not against a symlink planted at the
    leaf node itself after this call returns.

    Same residual as verify_router_runtime_base() (see its docstring for the full statement):
    this function's checks are pathname-based fail-fast pre-checks, not the atomic security
    boundary. That boundary is the router process (3c), which must hold the hierarchy via
    fd-relative traversal from a trusted anchor rather than re-resolving pathnames.
    """
    resolved_base = verify_router_runtime_base()
    d = _router_runtime_dir_from(resolved_base)
    return d, d / ROUTER_SOCK_NAME, d / ROUTER_LOCK_NAME


def ensure_router_runtime_dir() -> Path:
    """Create-or-verify the router's full runtime location, shallowest level first: the
    per-uid base directory, then the hash subdir inside it. This is the ONLY sanctioned way
    to prepare the directory router_sock()/router_lock() live in — see
    ensure_owned_private_dir for why a plain mkdir(parents=True) is not safe here, and
    verify_router_runtime_base() for why the BASE itself cannot be trusted unexamined before
    that. Returns router_runtime_dir() — built from THE SAME single base resolution
    resolved_router_paths() (and, inside it, verify_router_runtime_base()) performed, not a
    fresh call to router_runtime_dir(), which would re-resolve the base independently and
    reopen the TOCTOU window resolved_router_paths()'s docstring describes.

    Forward note for 3c (the router process): call this BEFORE binding router_sock() or
    calling `daemon/singleton.py:acquire(router_lock(), ...)` — acquire() performs no
    ownership/symlink verification of its own — and open/bind both symlink-safely (e.g.
    O_NOFOLLOW) so a symlink planted after this check is refused rather than followed. If you
    also need the sock/lock paths, prefer calling resolved_router_paths() yourself instead of
    pairing this function with router_sock()/router_lock() — see those accessors' own forward
    notes for why mixing the two is exactly the re-resolution this exists to avoid.
    """
    d, _sock, _lock = resolved_router_paths()
    for level in (d.parent, d):
        ensure_owned_private_dir(level)
    return d
