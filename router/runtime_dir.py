"""The router's SECURE socket/lock establishment — the atomic boundary slice 3b deferred here.

`paths.verify_router_runtime_base()` / `resolved_router_paths()` verify the runtime LOCATION at the
PATHNAME level and are honest that this is a racy, fail-fast pre-check (see those docstrings): the
verified base can be swapped by an attacker who controls an ancestor in the window between VERIFY
and USE. This module closes that window by re-establishing and HOLDING the hierarchy via
FD-RELATIVE, symlink-refusing operations:

  * Start from the verified resolved base and walk DOWN to the leaf `<base>/nelix-<uid>/<hash>`,
    opening EACH component with `os.open(name, O_RDONLY|O_DIRECTORY|O_NOFOLLOW, dir_fd=parent_fd)`.
    O_NOFOLLOW makes a symlinked component RAISE (ELOOP) rather than be followed; opening relative
    to the parent's FD (not by re-resolving a pathname) means the component that gets opened is the
    one that lived inside the directory we already hold, not whatever a concurrent rename now points
    the NAME at. Each opened dir fd is `fstat`ed and REQUIRED to be a real directory, owned by this
    uid, mode 0700 — fail closed on any mismatch.
  * The LEAF dir fd is HELD for the router's whole life. The lock and socket are then created
    RELATIVE to that held fd, so the directory they land in cannot be swapped after verification.

  * LOCK: `os.open("router.lock", O_RDWR|O_CREAT|O_NOFOLLOW, 0600, dir_fd=leaf_fd)` +
    `flock(LOCK_EX|LOCK_NB)`. One router per NELIX_HOME; a second loses the flock and this raises
    RouterLockHeld so the second exits cleanly. Taken BEFORE the socket is touched, so a losing
    second router never disturbs the winner's bound node. NOT `daemon/singleton.acquire` — it does a
    plain os.open with no O_NOFOLLOW (3b flagged this).

  * SOCKET: AF_UNIX bind() is pathname-based (no dir_fd / bind-at). We `os.unlink("router.sock",
    dir_fd=leaf_fd)` any stale node relative to the HELD leaf, then bind at the full verified path.
    WHAT CLOSES THE RESIDUAL BIND WINDOW: every directory on that path is one only we (or root)
    control — the base's whole ancestry is root/us-owned (verify_router_runtime_base), and both
    `nelix-<uid>` and `<hash>` were just fstat-verified 0700-and-ours through their held fds. 0700
    means no co-resident foreign uid can create an entry in, or rename, any of those directories, so
    none of the path's components can be swapped for a symlink between the unlinkat and the bind.
    The set of principals who could win that race is {root, this uid} — neither is in the threat
    model — so the window, though it exists (bind re-resolves the path rather than using the held
    fd), is not reachable by any foreign uid. That EXCLUSIVE-0700-ownership of the entire leaf
    ancestry below the verified base is the residual's bound. (fchdir(leaf_fd)+relative bind would
    remove the re-resolution entirely, but fchdir mutates process CWD globally; the brief prefers
    unlinkat + full-path bind under the held/verified parent, which this does.)
"""
import errno
import fcntl
import os
import socket
import stat
from dataclasses import dataclass

import paths

# O_NOFOLLOW on a symlinked component makes open FAIL rather than follow it — but the exact errno is
# platform-dependent: Linux reports ELOOP, while macOS with O_DIRECTORY reports ENOTDIR (the symlink
# node itself is not a directory). ENOTDIR also names a plain FILE planted where a directory belongs.
# Every one of these means the same thing here: the component is not a real directory we can safely
# traverse, so we refuse it closed rather than trying to interpret which flavour of not-a-directory
# it is. (FileNotFoundError is handled separately — that is "absent", which we create.)
_REFUSE_ERRNOS = (errno.ELOOP, errno.EMLINK, errno.ENOTDIR)


class RouterRuntimeInsecure(Exception):
    """A runtime-dir component failed the fd-relative symlink / owner / mode check. Fail closed —
    the router refuses to bind under a location it cannot prove it exclusively controls."""


class RouterLockHeld(Exception):
    """Another router already holds the per-NELIX_HOME exclusive lock. One router per NELIX_HOME;
    the loser exits cleanly rather than binding a second public socket over the first."""


@dataclass
class RuntimeDir:
    """The established, held router runtime location. `socket` is a BOUND (not yet listening)
    AF_UNIX SOCK_STREAM server socket the caller serves on; `lock_fd` and `dir_fd` are held for the
    router's life (the flock and the leaf-dir anchor)."""
    socket: socket.socket
    sock_path: str
    lock_fd: int
    dir_fd: int

    def close(self):
        """Release everything, in the reverse order it was acquired. Best-effort: teardown never
        raises. Unlinks the socket node relative to the still-held leaf fd (so a concurrent rename
        of the leaf can't redirect the unlink) before dropping the fds."""
        try:
            self.socket.close()
        except OSError:
            pass
        try:
            os.unlink(paths.ROUTER_SOCK_NAME, dir_fd=self.dir_fd)
        except OSError:
            pass
        # Closing the lock fd releases the flock (the last close of the open file description does).
        for fd in (self.lock_fd, self.dir_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def _open_dir_nofollow(name, parent_fd):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)


def _verify_dir_fd(fd, name, label):
    """fstat a freshly opened dir fd: real directory, owned by us, mode exactly 0700. Closes the fd
    and raises RouterRuntimeInsecure on any mismatch (fail closed)."""
    st = os.fstat(fd)
    problem = None
    if not stat.S_ISDIR(st.st_mode):
        problem = "is not a directory"
    elif st.st_uid != os.getuid():
        problem = f"is owned by uid {st.st_uid}, not this process's uid {os.getuid()}"
    elif st.st_mode & 0o777 != 0o700:
        problem = f"has mode {oct(st.st_mode & 0o777)}, not 0700"
    if problem is not None:
        os.close(fd)
        raise RouterRuntimeInsecure(f"{label} {name!r} {problem}; refusing to use it")


def _refuse_component(name, parent_fd, label, cause):
    """Turn an open failure that means "not a real directory we control" into RouterRuntimeInsecure,
    naming symlink vs non-directory precisely (best-effort lstat, relative to the held parent fd)."""
    kind = "is not a real directory"
    try:
        if stat.S_ISLNK(os.lstat(name, dir_fd=parent_fd).st_mode):
            kind = "is a symlink"
    except OSError:
        pass
    raise RouterRuntimeInsecure(f"{label} {name!r} {kind}; refusing to follow it") from cause


def _open_or_create_component(name, parent_fd, label):
    """Open `name` (a single path component) relative to `parent_fd`, symlink-refusing; create it
    0700 first if absent. Returns a verified, held dir fd."""
    try:
        fd = _open_dir_nofollow(name, parent_fd)
    except FileNotFoundError:
        # Create it 0700. umask is forced to 0o077 (which never strips owner bits) around the single
        # mkdir syscall so the directory lands at EXACTLY 0700 with no transiently-looser window a
        # concurrent verifier could observe — the same reasoning as paths.ensure_owned_private_dir.
        old_umask = os.umask(0o077)
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass                              # a concurrent creator won the race; open it below
        finally:
            os.umask(old_umask)
        try:
            fd = _open_dir_nofollow(name, parent_fd)
        except OSError as e:
            if e.errno in _REFUSE_ERRNOS:
                _refuse_component(name, parent_fd, label, e)
            raise
    except OSError as e:
        if e.errno in _REFUSE_ERRNOS:
            _refuse_component(name, parent_fd, label, e)
        raise
    _verify_dir_fd(fd, name, label)
    return fd


def establish() -> RuntimeDir:
    """Verify (pathname-level), then ATOMICALLY establish and HOLD the router's runtime location,
    bind its public socket, and take its exclusive lock. Returns a RuntimeDir the caller serves on.

    Raises RouterRuntimeInsecure if any component fails the fd-relative symlink/owner/mode check,
    RouterLockHeld if another router already holds the lock, ValueError if the socket path overflows
    sun_path, or paths.RouterRuntimeBaseRejected if the base itself is unsafe (pathname pre-check).
    """
    base = paths.verify_router_runtime_base()                  # resolved, ancestry-verified anchor
    d, sock_path, _lock_path = paths.resolved_router_paths()   # same single resolution; d under base
    rel_parts = d.relative_to(base).parts                      # ("nelix-<uid>", "<hash>")

    intermediate_fds = []
    leaf_fd = None
    lock_fd = None
    server_sock = None
    try:
        # The trusted anchor: the resolved base. O_NOFOLLOW guards its final component; its ancestry
        # was verified owned by root/us (which an attacker cannot swap) by verify_router_runtime_base.
        base_fd = os.open(str(base), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        intermediate_fds.append(base_fd)

        parent = base_fd
        for i, part in enumerate(rel_parts):
            label = "router per-uid runtime dir" if i == 0 else "router runtime dir"
            fd = _open_or_create_component(part, parent, label)
            if i < len(rel_parts) - 1:
                intermediate_fds.append(fd)
            else:
                leaf_fd = fd
            parent = fd

        # LOCK first — before the socket is touched — relative to the held leaf fd, O_NOFOLLOW so a
        # planted symlink at router.lock is refused rather than followed.
        lock_fd = os.open(paths.ROUTER_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600,
                          dir_fd=leaf_fd)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RouterLockHeld(
                f"another router already holds {sock_path.parent / paths.ROUTER_LOCK_NAME}; "
                f"this router is exiting") from e

        # SOCKET: refuse an over-long path BEFORE unlinking anything (an over-long path would fail
        # the bind AFTER destroying the node — mirrors _make_unix_server's pre-check).
        problem = paths.sun_path_overflow(sock_path)
        if problem:
            raise ValueError(f"router cannot bind its public socket: {problem}")
        # Unlink any stale node relative to the HELD leaf fd (not by re-resolving the path), then
        # bind at the full verified path. See the module docstring for what bounds the residual.
        try:
            os.unlink(paths.ROUTER_SOCK_NAME, dir_fd=leaf_fd)
        except FileNotFoundError:
            pass
        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)                # socket node lands 0600 (0666 & ~0177)
        try:
            server_sock.bind(str(sock_path))
        finally:
            os.umask(old_umask)
        os.chmod(sock_path, 0o600)                 # belt-and-braces on top of the 0700 dir + umask

        handle = RuntimeDir(socket=server_sock, sock_path=str(sock_path),
                            lock_fd=lock_fd, dir_fd=leaf_fd)
        # Ownership transferred to the handle; clear the locals so the except/finally below does not
        # tear them down on the success path.
        leaf_fd = lock_fd = None
        server_sock = None
        return handle
    except BaseException:
        if server_sock is not None:
            server_sock.close()
        if lock_fd is not None:
            os.close(lock_fd)
        if leaf_fd is not None:
            os.close(leaf_fd)
        raise
    finally:
        for fd in intermediate_fds:
            try:
                os.close(fd)
            except OSError:
                pass
