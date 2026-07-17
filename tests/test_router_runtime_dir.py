"""nelix-3rm slice 3c.1 Part B: the router's SECURE fd-relative socket/lock establishment — the
atomic boundary slice 3b deferred here. `paths` verifies the runtime location at the (racy)
PATHNAME level; runtime_dir.establish() re-establishes and HOLDS the hierarchy via fd-relative,
symlink-refusing operations (os.open(O_DIRECTORY|O_NOFOLLOW, dir_fd=...)) so the verify->use TOCTOU
is closed.

These tests bind at the PRODUCTION router_sock() (a real AF_UNIX bind), plant a real symlink at a
runtime-dir component (O_NOFOLLOW must refuse it), and prove the lock is exclusive."""
import os
import socket
import stat
import threading

import pytest

import paths
from router import runtime_dir as rd


def _cleanup(handle):
    if handle is not None:
        handle.close()


def test_establish_binds_socket_0600_holds_lock_and_leaf_fd():
    handle = rd.establish()
    try:
        d, sock_path, _lock_path = paths.resolved_router_paths()
        # The socket node exists at the production path, is a socket, mode 0600.
        st = os.lstat(sock_path)
        assert stat.S_ISSOCK(st.st_mode)
        assert st.st_mode & 0o777 == 0o600
        # The HELD leaf dir fd names exactly the resolved leaf directory.
        assert os.fstat(handle.dir_fd).st_ino == os.stat(d).st_ino
        assert stat.S_ISDIR(os.fstat(handle.dir_fd).st_mode)
    finally:
        _cleanup(handle)


def test_bound_socket_actually_serves():
    handle = rd.establish()
    try:
        _d, sock_path, _lock = paths.resolved_router_paths()
        handle.socket.listen(8)

        def _accept_once():
            conn, _ = handle.socket.accept()
            with conn:
                conn.sendall(b"pong")

        t = threading.Thread(target=_accept_once, daemon=True)
        t.start()
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(str(sock_path))
        assert c.recv(4) == b"pong"
        c.close()
        t.join(timeout=5)
    finally:
        _cleanup(handle)


def test_establish_refuses_a_symlinked_leaf_component():
    d, _sock, _lock = paths.resolved_router_paths()
    # Build a real, 0700-ours per-uid parent, then plant a SYMLINK where the hash leaf belongs.
    d.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(d.parent, 0o700)
    target = d.parent / "decoy-real-dir"
    target.mkdir(mode=0o700, exist_ok=True)
    if d.exists() or d.is_symlink():
        try:
            os.unlink(d)
        except (IsADirectoryError, OSError):
            pass
    os.symlink(str(target), str(d))          # the leaf is now a symlink -> O_NOFOLLOW must refuse it
    try:
        with pytest.raises(rd.RouterRuntimeInsecure):
            rd.establish()
    finally:
        os.unlink(d)


def test_establish_refuses_a_wrong_mode_leaf():
    d, _sock, _lock = paths.resolved_router_paths()
    d.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(d.parent, 0o700)
    d.mkdir(mode=0o755, exist_ok=True)
    os.chmod(d, 0o755)                        # group/world-accessible: not ours-private, refuse
    try:
        with pytest.raises(rd.RouterRuntimeInsecure):
            rd.establish()
    finally:
        os.chmod(d, 0o700)
        os.rmdir(d)


def test_second_establish_loses_the_exclusive_lock():
    first = rd.establish()
    try:
        with pytest.raises(rd.RouterLockHeld):
            rd.establish()
    finally:
        _cleanup(first)


def test_close_releases_the_lock_so_a_fresh_establish_succeeds():
    first = rd.establish()
    first.close()
    second = rd.establish()                    # the lock was released, so this wins cleanly
    try:
        assert os.lstat(paths.resolved_router_paths()[1]).st_mode & 0o777 == 0o600
    finally:
        _cleanup(second)
