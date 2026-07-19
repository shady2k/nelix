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
import time
import types
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
    # A path under NELIX_HOME (like the old rpc_sock) overflows.
    old_style = paths.nelix_root() / "rpc.sock"
    assert paths.sun_path_overflow(old_style) is not None      # the OLD location overflows
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


# --- single resolution: verify and construct must agree on ONE answer -------------------
# [review: double-resolution TOCTOU — verify resolves the base, construction re-resolves it]
#
# Before the fix, ensure_router_runtime_dir() called verify_router_runtime_base() (which
# resolves the base once to CHECK it) and then separately called router_runtime_dir() (which
# resolves the SAME configured base again to BUILD the path). A base that is a symlink can
# answer safely the first time and point somewhere else the second — the window between the
# two independent resolve() calls. The fix must resolve the base exactly ONCE per top-level
# call and reuse that single value for both the check and the construction.

def test_ensure_router_runtime_dir_resolves_the_base_exactly_once(monkeypatch, tmp_path):
    """Prove single resolution deterministically rather than relying on a real race: make
    Path.resolve() answer DIFFERENTLY on the second call than the first for the configured
    base (standing in for a symlink repointed between verify-time and construct-time). If
    ensure_router_runtime_dir() still resolves twice, the directory it builds lands under the
    SECOND answer; if it resolves once (the fix), the directory lands under the FIRST — the
    only answer verify_router_runtime_base() ever saw and checked."""
    target_a = tmp_path / "target-a"; target_a.mkdir(mode=0o700)
    target_b = tmp_path / "target-b"; target_b.mkdir(mode=0o700)
    base_symlink = tmp_path / "swappable-base"
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(base_symlink))
    importlib.reload(paths)

    real_resolve = Path.resolve
    calls = []

    def fake_resolve(self, *a, **kw):
        if self == base_symlink:
            calls.append(1)
            return target_a if len(calls) == 1 else target_b
        return real_resolve(self, *a, **kw)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    d = paths.ensure_router_runtime_dir()

    assert target_a in d.parents, (
        f"expected the dir under the FIRST (verified) resolution {target_a}, got {d}")
    assert target_b not in d.parents, (
        f"dir was built under a SECOND, independent resolution {target_b} — "
        f"verify and construct disagree (TOCTOU)")
    assert len(calls) == 1, (
        f"base_symlink was resolved {len(calls)} times, not once — an extra resolution "
        f"escapes the single-resolution guarantee even if this particular assertion above "
        f"still happened to pass")


def test_resolved_router_paths_matches_the_pure_accessors_in_the_normal_case(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(tmp_path))
    importlib.reload(paths)
    d, sock, lock = paths.resolved_router_paths()
    assert d == paths.router_runtime_dir()
    assert sock == paths.router_sock()
    assert lock == paths.router_lock()
    assert sock == d / "router.sock"
    assert lock == d / "router.lock"


def test_resolved_router_paths_resolves_the_base_exactly_once(monkeypatch, tmp_path):
    """The single-resolution helper itself must only resolve once — it is the building block
    ensure_router_runtime_dir() is supposed to use instead of mixing independent re-resolutions."""
    target_a = tmp_path / "target-a"; target_a.mkdir(mode=0o700)
    target_b = tmp_path / "target-b"; target_b.mkdir(mode=0o700)
    base_symlink = tmp_path / "swappable-base"
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(base_symlink))
    importlib.reload(paths)

    real_resolve = Path.resolve
    calls = []

    def fake_resolve(self, *a, **kw):
        if self == base_symlink:
            calls.append(1)
            return target_a if len(calls) == 1 else target_b
        return real_resolve(self, *a, **kw)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    d, sock, lock = paths.resolved_router_paths()
    assert target_a in d.parents
    assert target_b not in d.parents
    assert sock.parent == d and lock.parent == d
    assert len(calls) == 1, (
        f"base_symlink was resolved {len(calls)} times, not once — an extra resolution "
        f"escapes the single-resolution guarantee even if this particular assertion above "
        f"still happened to pass")


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


# --- base OWNERSHIP: the sticky bit stops other users, not the base's own owner --------
# [review: an attacker-OWNED sticky (or nominally-private) base still hijacks]
#
# verify_router_runtime_base()'s mode check alone is not enough: sticky only denies rename/
# delete rights to everyone EXCEPT the node's owner (and root). A base owned by some other,
# non-root uid passes the old mode-only check whether or not it is world-writable — its
# owner can rename our already-verified per-uid dir out from under us regardless. A test
# process cannot chown a directory to a foreign uid, so these simulate "owned by someone
# else" the same way test_ensure_owned_private_dir_rejects_foreign_owner_rather_than_adopting
# does: make the CODE's own os.getuid() answer differently, so the real (test-owned)
# directory reads as foreign from the code's point of view.

def test_ensure_router_runtime_dir_rejects_a_sticky_base_owned_by_another_uid(monkeypatch, tmp_path):
    """1777 (sticky, world-writable) is exactly how the real /tmp is configured, and the old
    check accepts any sticky base regardless of who owns it. An attacker who owns the base
    can still rename/replace our verified per-uid dir — sticky only stops OTHER users."""
    base = tmp_path / "attacker-sticky-base"; base.mkdir(mode=0o777)
    os.chmod(base, 0o1777)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 12345)
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(base))
    importlib.reload(paths)
    with pytest.raises(paths.RouterRuntimeBaseRejected, match="owner"):
        paths.ensure_router_runtime_dir()


def test_ensure_router_runtime_dir_rejects_a_private_base_owned_by_another_uid(monkeypatch, tmp_path):
    """The more dangerous gap: a 0700 base is not group/world-writable, so it never even
    reached the sticky-bit branch — the old code trusted ANY "private-looking" base with
    zero ownership check. An attacker-owned "private" base is exactly as attacker-controlled
    as an attacker-owned world-writable one."""
    base = tmp_path / "attacker-private-base"; base.mkdir(mode=0o700)
    os.chmod(base, 0o700)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 12345)
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(base))
    importlib.reload(paths)
    with pytest.raises(paths.RouterRuntimeBaseRejected, match="owner"):
        paths.ensure_router_runtime_dir()


def test_ensure_router_runtime_dir_accepts_a_root_owned_sticky_base_even_when_not_ours(monkeypatch, tmp_path):
    """Root-owned is always safe no matter which uid we run as — root can already do
    anything to our files, so a root-owned sticky base (the real /tmp's actual ownership)
    must be accepted by the carve-out for uid 0, not merely by the "we own it" branch. A test
    process can't chown to root, so fake it at the os.stat layer: only the base's OWNERSHIP
    is faked (st_uid=0), while os.getuid() keeps answering this process's real uid — so the
    only way this can pass is the root carve-out, never the "it's ours" one."""
    base = tmp_path / "root-sticky-base"; base.mkdir(mode=0o777)
    os.chmod(base, 0o1777)
    resolved_base = base.resolve()   # computed BEFORE patching os.stat, to avoid recursion
    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if Path(path) == resolved_base:
            return types.SimpleNamespace(st_mode=st.st_mode, st_uid=0)
        return st

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(base))
    importlib.reload(paths)
    d = paths.ensure_router_runtime_dir()
    assert d == paths.router_runtime_dir()


# --- ANCESTRY: an attacker-owned ancestor can replace an otherwise-fine base ------------
# [review: ancestry TOCTOU — verify_router_runtime_base() checked only the base's own stat]
#
# A base that is itself victim-owned, 0700, and passes every check above can still be pulled
# out from under us if some ANCESTOR of it is attacker-owned and writable: the attacker can
# rename/replace the base after our stat (they only need write on the base's PARENT to do
# that), and our subsequent pathname-based mkdir lands under the replacement. Leaf-only
# checks — even the correct owner/sticky rule applied only to the base itself — do not see
# this; the walk must cover every component from the resolved base up to "/".

def test_ensure_router_runtime_dir_rejects_a_base_whose_ancestor_is_owned_by_another_uid(
    monkeypatch, tmp_path
):
    """attacker-parent/base: the base itself is victim-owned 0700 (passes its own check), but
    the PARENT directory is attacker-owned — the attacker can rename `base` out from under us
    after verification. Simulated by spoofing os.stat's st_uid for just the parent path (a
    test process cannot really chown to a foreign uid); the base's real stat is untouched, so
    this proves the ancestor's ownership is what trips the rejection, not the base's own."""
    attacker_parent = tmp_path / "attacker-parent"
    attacker_parent.mkdir(mode=0o755)
    base = attacker_parent / "base"
    base.mkdir(mode=0o700)
    resolved_parent = attacker_parent.resolve()
    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if Path(path) == resolved_parent:
            return types.SimpleNamespace(st_mode=st.st_mode, st_uid=st.st_uid + 12345)
        return st

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(base))
    importlib.reload(paths)

    with pytest.raises(paths.RouterRuntimeBaseRejected) as excinfo:
        paths.ensure_router_runtime_dir()
    assert str(resolved_parent) in str(excinfo.value), (
        "rejection must name the offending ancestor, not just say something failed")
    assert "owner" in str(excinfo.value)


def test_ensure_router_runtime_dir_rejects_a_base_under_a_non_sticky_writable_ancestor(
    monkeypatch, tmp_path
):
    """The other half of the ancestry hazard: an ancestor that is merely group/world-writable
    without the sticky bit lets ANY co-resident (not just its owner) rename/replace the base
    out from under us, exactly like the base-level check above but one level up the tree."""
    writable_parent = tmp_path / "writable-parent"
    writable_parent.mkdir(mode=0o777)
    os.chmod(writable_parent, 0o777)   # world-writable, no sticky bit
    base = writable_parent / "base"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("NELIX_RUNTIME_BASE", str(base))
    importlib.reload(paths)

    with pytest.raises(paths.RouterRuntimeBaseRejected) as excinfo:
        paths.ensure_router_runtime_dir()
    assert str(writable_parent.resolve()) in str(excinfo.value)
    assert "sticky" in str(excinfo.value)


def test_verify_router_runtime_base_accepts_the_real_tmp_ancestry_chain(monkeypatch):
    """The ancestry walk must not reject the real default base: '/' is root-owned 0755,
    '/private' (macOS) is root-owned 0755, and '/private/tmp' (what '/tmp' resolves to) is
    root-owned sticky 1777 — every component passes the same per-component trust rule the
    walk applies to the base itself."""
    monkeypatch.delenv("NELIX_RUNTIME_BASE", raising=False)
    importlib.reload(paths)
    resolved = paths.verify_router_runtime_base()   # must not raise
    assert resolved.is_absolute()


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


def test_ensure_owned_private_dir_umask_window_is_not_interleaved_across_threads(monkeypatch, tmp_path):
    """os.umask is process-global, not per-thread: two concurrent same-process creators each
    doing umask(0o077) ... mkdir ... umask(restore) can interleave without a lock, leaving
    the ambient umask at 0o077 (or some other wrong value) for the DURATION of one thread's
    window while it is actually a different thread's turn — or leaving it permanently wrong
    if a restore lands between the other thread's open and its own restore. Force a real
    attempt at overlap: make the mkdir syscall itself pause inside the critical section so a
    concurrently-running rival has every opportunity to slip its own umask(0o077) call in
    before the first thread restores. With the lock in force this must never happen: every
    thread's OPEN (umask(0o077)) must be immediately followed, in the GLOBAL call sequence,
    by that SAME thread's own RESTORE — never by the other thread's open."""
    real_mkdir = os.mkdir

    def slow_mkdir(path, mode):
        time.sleep(0.05)   # widen the window a missing lock would let a rival exploit
        return real_mkdir(path, mode)

    monkeypatch.setattr(os, "mkdir", slow_mkdir)

    events = []   # (thread_ident, requested_mask) in the order os.umask was actually called
    real_umask = os.umask

    def spying_umask(mask):
        old = real_umask(mask)
        events.append((threading.get_ident(), mask))
        return old

    monkeypatch.setattr(os, "umask", spying_umask)

    targets = [tmp_path / "race-a", tmp_path / "race-b"]
    results = []

    def go(p):
        try:
            results.append(paths.ensure_owned_private_dir(p))
        except Exception as e:                     # noqa: BLE001 - captured for the assertion below
            results.append(e)

    threads = [threading.Thread(target=go, args=(p,)) for p in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(isinstance(r, Path) for r in results), results
    assert len(events) == 4, events   # 2 threads x (open, restore)

    opens = [i for i, (_, mask) in enumerate(events) if mask == 0o077]
    assert len(opens) == 2, events
    for i in opens:
        # the very next event in the GLOBAL sequence must be THIS thread's own restore —
        # never the other thread's open sneaking in first.
        assert i + 1 < len(events), events
        this_thread = events[i][0]
        next_thread, next_mask = events[i + 1]
        assert next_thread == this_thread and next_mask != 0o077, (
            f"thread {this_thread}'s umask(0o077) window was interleaved with another "
            f"thread's call before its own restore: {events}")


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
