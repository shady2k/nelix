"""Location + security primitives for the router's public unix socket [nelix-3rm.1].

The router PROCESS is a later slice (3c); this file exercises only what 3b delivers: the
path accessors (router_runtime_dir/router_sock/router_lock), the create-or-verify security
helper, and — the coverage hole this bead exists to close — a test that actually BINDS an
AF_UNIX socket at the production router_sock() path.
"""
import hashlib
import importlib
import os
import shutil
import socket
import stat
import threading
from pathlib import Path

import pytest

import paths


# --- location shape: short, per-uid, hash-keyed -----------------------------------------

def test_router_sock_and_lock_live_in_the_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(tmp_path))
    importlib.reload(paths)
    d = paths.router_runtime_dir()
    assert paths.router_sock() == d / "router.sock"
    assert paths.router_lock() == d / "router.lock"


def test_router_runtime_dir_is_not_under_nelix_root(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(tmp_path))
    monkeypatch.setenv("NELIX_HOME", str(tmp_path / "home"))
    importlib.reload(paths)
    assert paths.nelix_root() not in paths.router_runtime_dir().parents


def test_router_runtime_dir_is_namespaced_per_uid(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(tmp_path))
    importlib.reload(paths)
    assert f"nelix-{os.getuid()}" in paths.router_runtime_dir().parts


def test_router_runtime_dir_is_keyed_by_a_hash_of_the_canonical_root(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(tmp_path))
    monkeypatch.setenv("NELIX_HOME", str(tmp_path / "home"))
    importlib.reload(paths)
    expected = hashlib.sha256(str(paths.nelix_root()).encode()).hexdigest()[:paths.ROUTER_HASH_LEN]
    assert paths.router_runtime_dir().name == expected


def test_same_nelix_home_gives_the_same_location(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(tmp_path))
    monkeypatch.setenv("NELIX_HOME", str(tmp_path / "home"))
    importlib.reload(paths)
    a = paths.router_runtime_dir()
    importlib.reload(paths)
    b = paths.router_runtime_dir()
    assert a == b


def test_different_nelix_home_gives_a_different_location(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(tmp_path))
    monkeypatch.setenv("NELIX_HOME", str(tmp_path / "home-a"))
    importlib.reload(paths)
    a = paths.router_runtime_dir()
    monkeypatch.setenv("NELIX_HOME", str(tmp_path / "home-b"))
    importlib.reload(paths)
    b = paths.router_runtime_dir()
    assert a != b


def test_symlink_alias_home_resolves_to_the_same_router_location(monkeypatch, tmp_path):
    """nelix_root() canonicalises first, so a symlink alias must hash to the SAME location as
    its canonical target — the whole reason the key is built from nelix_root(), not raw
    $NELIX_HOME text."""
    real = tmp_path / "real"; real.mkdir()
    alias = tmp_path / "alias"; alias.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(tmp_path / "rt"))

    monkeypatch.setenv("NELIX_HOME", str(real))
    importlib.reload(paths)
    via_real = paths.router_runtime_dir()

    monkeypatch.setenv("NELIX_HOME", str(alias))
    importlib.reload(paths)
    assert paths.router_runtime_dir() == via_real


def test_router_sock_path_length_is_independent_of_nelix_home_depth(monkeypatch, tmp_path):
    """The whole point of the hash-keyed location: a deep $NELIX_HOME that would overflow
    sun_path under the OLD (nelix_root-relative) scheme must not touch the router's socket
    path at all, because the key is a fixed-width hash, not the home's text."""
    deep = tmp_path / ("d" * 200)
    monkeypatch.setenv("NELIX_HOME", str(deep))
    importlib.reload(paths)
    assert paths.sun_path_overflow(paths.rpc_sock()) is not None      # the OLD location overflows
    assert paths.sun_path_overflow(paths.router_sock()) is None      # the router's does not


# --- NELIX_RUNTIME_BASE: documented, overridable, short default ------------------------

def test_runtime_base_defaults_to_the_short_slash_tmp(monkeypatch):
    monkeypatch.delenv("NELIX_RUNTIME_BASE", raising=False)
    importlib.reload(paths)
    assert paths.router_runtime_base() == Path("/tmp")


def test_runtime_base_env_override_is_honoured(monkeypatch, tmp_path):
    custom = tmp_path / "custom-base"; custom.mkdir()
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(custom))
    importlib.reload(paths)
    assert paths.router_runtime_base() == custom
    assert custom in paths.router_runtime_dir().parents


def test_router_runtime_dir_uses_the_resolved_base(monkeypatch, tmp_path):
    """A later swap of a symlinked base must not redirect callers away from what
    ensure_router_runtime_dir() verified and created — so the per-uid dir is always built
    under Path.resolve()'d base, not the base's raw (possibly symlinked) text."""
    real = tmp_path / "real-base"; real.mkdir()
    alias = tmp_path / "alias-base"; alias.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(alias))
    importlib.reload(paths)
    assert real in paths.router_runtime_dir().parents
    assert alias not in paths.router_runtime_dir().parts


# --- NELIX_RUNTIME_BASE must itself be verified safe [review: hijack via unsafe base] ---

def test_ensure_router_runtime_dir_rejects_a_relative_runtime_base(monkeypatch):
    monkeypatch.setenv("NELIX_RUNTIME_BASE", "relative/base")
    importlib.reload(paths)
    with pytest.raises(paths.RouterRuntimeBaseRejected, match="relative"):
        paths.ensure_router_runtime_dir()


def test_ensure_router_runtime_dir_rejects_a_non_sticky_world_writable_base(monkeypatch, tmp_path):
    """The compromise path both reviewers found: a world-writable base WITHOUT the sticky
    bit lets a co-resident attacker rename/replace an already-verified per-uid dir, because
    deleting/renaming a node only needs write on its PARENT — exactly what the sticky bit
    prevents. Must raise loudly instead of quietly building a socket/lock there."""
    base = tmp_path / "unsafe-base"; base.mkdir(mode=0o777); os.chmod(base, 0o777)
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(base))
    importlib.reload(paths)
    with pytest.raises(paths.RouterRuntimeBaseRejected, match="sticky"):
        paths.ensure_router_runtime_dir()


def test_ensure_router_runtime_dir_rejects_a_missing_base(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(tmp_path / "does-not-exist"))
    importlib.reload(paths)
    with pytest.raises(paths.RouterRuntimeBaseRejected):
        paths.ensure_router_runtime_dir()


def test_ensure_router_runtime_dir_accepts_a_private_owned_base(monkeypatch, tmp_path):
    base = tmp_path / "private-base"; base.mkdir(mode=0o700); os.chmod(base, 0o700)
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(base))
    importlib.reload(paths)
    d = paths.ensure_router_runtime_dir()
    assert d == paths.router_runtime_dir()


def test_ensure_router_runtime_dir_accepts_a_sticky_world_writable_base(monkeypatch, tmp_path):
    """A world-writable base WITH the sticky bit (like the real /tmp) is exactly as safe as
    the default and must still work."""
    base = tmp_path / "sticky-base"; base.mkdir(mode=0o777)
    os.chmod(base, 0o1777)
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(base))
    importlib.reload(paths)
    d = paths.ensure_router_runtime_dir()
    assert d == paths.router_runtime_dir()


def test_ensure_router_runtime_dir_still_works_with_the_default_tmp_base(monkeypatch):
    """The default base itself must clear the new safety gate: /tmp resolves to /private/tmp
    on macOS, and IT is the directory that must be verified sticky, not the symlink /tmp."""
    monkeypatch.delenv("NELIX_RUNTIME_BASE", raising=False)
    importlib.reload(paths)
    d = paths.ensure_router_runtime_dir()
    try:
        assert d == paths.router_runtime_dir()
    finally:
        shutil.rmtree(d, ignore_errors=True)   # leave the shared per-uid parent alone


# --- create-or-verify security helper ---------------------------------------------------

def test_ensure_router_runtime_dir_creates_0700_owned_by_current_uid(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(tmp_path))
    importlib.reload(paths)
    d = paths.ensure_router_runtime_dir()
    assert d == paths.router_runtime_dir()
    for level in (d, d.parent):
        st = os.lstat(level)
        assert stat.S_ISDIR(st.st_mode), level
        assert st.st_uid == os.getuid(), level
        assert oct(st.st_mode & 0o777) == "0o700", level


def test_ensure_router_runtime_dir_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(tmp_path))
    importlib.reload(paths)
    d1 = paths.ensure_router_runtime_dir()
    d2 = paths.ensure_router_runtime_dir()
    assert d1 == d2
    assert oct(os.lstat(d1).st_mode & 0o777) == "0o700"


def test_ensure_owned_private_dir_rejects_a_symlink(tmp_path):
    real = tmp_path / "real-target"; real.mkdir(mode=0o700); os.chmod(real, 0o700)
    link = tmp_path / "link"; link.symlink_to(real, target_is_directory=True)
    with pytest.raises(paths.RouterRuntimeDirRejected, match="symlink"):
        paths.ensure_owned_private_dir(link)
    # must not have been "fixed" or replaced
    assert link.is_symlink()


def test_ensure_owned_private_dir_rejects_a_plain_file(tmp_path):
    f = tmp_path / "not-a-dir"; f.write_text("x")
    with pytest.raises(paths.RouterRuntimeDirRejected):
        paths.ensure_owned_private_dir(f)


def test_ensure_owned_private_dir_rejects_wrong_mode_rather_than_adopting(tmp_path):
    d = tmp_path / "loose"; d.mkdir(mode=0o755); os.chmod(d, 0o755)
    with pytest.raises(paths.RouterRuntimeDirRejected, match="mode"):
        paths.ensure_owned_private_dir(d)
    # rejected, not corrected: mode must be untouched
    assert oct(d.stat().st_mode & 0o777) == "0o755"


def test_ensure_owned_private_dir_rejects_foreign_owner_rather_than_adopting(monkeypatch, tmp_path):
    """A test process can't chown to another uid, so simulate the mismatch from the other
    side: make the CODE believe its own uid is different, which makes the real (test-owned)
    directory read as foreign — exactly the comparison ensure_owned_private_dir performs."""
    d = tmp_path / "mine"; d.mkdir(mode=0o700); os.chmod(d, 0o700)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 12345)
    with pytest.raises(paths.RouterRuntimeDirRejected, match="owner"):
        paths.ensure_owned_private_dir(d)
    # rejected, not adopted: still owned by the real uid, unchanged mode
    assert d.stat().st_uid == real_uid
    assert oct(d.stat().st_mode & 0o777) == "0o700"


def test_ensure_owned_private_dir_accepts_a_correct_existing_dir(tmp_path):
    d = tmp_path / "already-fine"; d.mkdir(mode=0o700); os.chmod(d, 0o700)
    assert paths.ensure_owned_private_dir(d) == d


def test_ensure_owned_private_dir_is_race_safe_under_concurrent_creation(tmp_path):
    """mkdir is atomic; a concurrent creator's FileExistsError must fall through to verify,
    not blow up. Two threads racing to create the SAME not-yet-existing directory must both
    return successfully with the directory left 0700."""
    target = tmp_path / "racing"
    results = []

    def go():
        try:
            results.append(paths.ensure_owned_private_dir(target))
        except Exception as e:                     # noqa: BLE001 - captured for the assertion below
            results.append(e)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(isinstance(r, Path) for r in results), results
    assert oct(target.stat().st_mode & 0o777) == "0o700"


def test_ensure_owned_private_dir_mkdir_reaches_0700_even_under_a_hostile_umask(monkeypatch, tmp_path):
    """Regression for the reviewed umask race: under an ambient umask that strips OWNER bits
    (e.g. 0o700), a plain `os.mkdir(path, 0o700)` transiently creates the dir at mode 0000
    before the follow-up chmod fixes it — a window a concurrent same-uid verifier's lstat
    could observe and wrongly reject. The fix forces umask 0o077 (which never strips owner
    bits) around the mkdir call itself, so the mode mkdir(2) lands on is already 0700 with no
    reliance on the chmod that follows. Proven here by spying on os.chmod: it must already
    see 0700 the instant it is called, before it does anything itself.
    """
    path = tmp_path / "hostile"
    modes_seen_before_chmod = []
    real_chmod = os.chmod

    def spying_chmod(p, mode):
        modes_seen_before_chmod.append(stat.S_IMODE(os.lstat(p).st_mode))
        return real_chmod(p, mode)

    monkeypatch.setattr(os, "chmod", spying_chmod)
    old_umask = os.umask(0o700)   # strips owner bits from any raw mkdir(mode) request
    try:
        paths.ensure_owned_private_dir(path)
    finally:
        os.umask(old_umask)

    assert modes_seen_before_chmod == [0o700], (
        "mkdir(2) itself must already land on 0700 under a hostile ambient umask; seeing "
        "anything else means the internal umask override isn't wrapping the mkdir call")


# --- the coverage hole: an actual bind at the production router_sock() path ------------

def test_router_sock_binds_a_real_af_unix_socket_at_the_production_path():
    """Closes the hole nelix-3rm.1 exists for: before this, no test bound a socket at the
    production router_sock() path. Deliberately does NOT override NELIX_RUNTIME_BASE — the
    autouse isolate_nelix_home fixture already points $NELIX_HOME at a deep pytest tmp dir,
    which is exactly the "realistic, even deep $NELIX_HOME" case the location has to survive
    while still binding under the short default /tmp base.
    """
    importlib.reload(paths)
    d = paths.ensure_router_runtime_dir()
    sock_path = paths.router_sock()
    try:
        assert sock_path.parent == d
        assert paths.sun_path_overflow(sock_path) is None

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(str(sock_path))
            s.listen(1)
            assert sock_path.exists()
            assert stat.S_ISSOCK(os.lstat(sock_path).st_mode)
        finally:
            s.close()
    finally:
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(d, ignore_errors=True)   # clean the hash subdir we created for this test;
                                                # leave the shared per-uid parent alone
