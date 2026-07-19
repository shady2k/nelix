import fnmatch
import importlib
import os
from pathlib import Path


import paths


def test_layout_all_under_nelix_home(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths)
    root = tmp_path
    assert paths.nelix_root() == root
    assert paths.config_path() == root / "nelix.toml"
    assert paths.state_file() == root / ".active.json"
    assert paths.sessions_root() == root / "sessions"
    assert paths.logs_dir() == root / "logs"
    assert paths.daemon_log("20260624-170000", 4242) == root / "logs" / "daemon-20260624-170000-4242.log"
    assert paths.daemon_latest() == root / "logs" / "daemon-latest.log"


def test_default_root_is_dot_nelix_in_the_users_home(monkeypatch):
    """NELIX_HOME unset => ~/.nelix. Not XDG: macOS normally has no XDG_RUNTIME_DIR."""
    monkeypatch.delenv("NELIX_HOME", raising=False)
    importlib.reload(paths)
    assert paths.nelix_root() == Path.home().resolve() / ".nelix"


def test_the_isolation_fixture_is_in_force():
    """No test may resolve the root to the operator's real ~/.nelix — conftest's autouse
    isolate_nelix_home fixture guarantees it. This test exists to give that fixture teeth:
    delete the fixture and this goes red, which is the only reason to trust the rest of the
    suite is not writing PTY dumps into somebody's home directory. (It was, until 9a4.7 —
    see the fixture's docstring for the measurement.)"""
    assert paths.nelix_root() != Path.home().resolve() / ".nelix"


def test_root_names_no_harness_home(monkeypatch):
    """The point of the slice: the core's state is not inside any harness's directory. A
    HERMES_HOME in the environment must not move — or even reach — the nelix root."""
    monkeypatch.delenv("NELIX_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", "/tmp/some-hermes-home")
    importlib.reload(paths)
    root = paths.nelix_root()
    assert "hermes" not in str(root).lower()
    assert "workspace" not in str(root).lower()
    assert root == Path.home().resolve() / ".nelix"


def test_env_override_beats_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path / "elsewhere"))
    importlib.reload(paths)
    assert paths.nelix_root() == tmp_path / "elsewhere"


def test_blank_env_falls_back_to_the_default(monkeypatch):
    """NELIX_HOME='' (or whitespace) is an unset var, not a request to root the layout at ''."""
    monkeypatch.setenv("NELIX_HOME", "   ")
    importlib.reload(paths)
    assert paths.nelix_root() == Path.home().resolve() / ".nelix"


def test_env_override_expands_a_tilde(monkeypatch):
    monkeypatch.setenv("NELIX_HOME", "~/.nelix-alt")
    importlib.reload(paths)
    assert paths.nelix_root() == Path.home().resolve() / ".nelix-alt"


# --- canonicalisation: one directory must not read as two roots ------------------------

def test_symlink_alias_resolves_to_the_same_root(monkeypatch, tmp_path):
    """~/.nelix, /Users/x/.nelix and a symlink alias must name ONE root. Root identity is
    daemon identity (daemon.lock + rpc.sock live under it), so two spellings of one directory
    must never read as two daemons."""
    real = tmp_path / "real"; real.mkdir()
    alias = tmp_path / "alias"; alias.symlink_to(real, target_is_directory=True)

    monkeypatch.setenv("NELIX_HOME", str(real))
    importlib.reload(paths)
    via_real = paths.nelix_root()
    lock_via_real = paths.daemon_lock()

    monkeypatch.setenv("NELIX_HOME", str(alias))
    importlib.reload(paths)
    assert paths.nelix_root() == via_real, "a symlink alias resolved to a second root"
    assert paths.daemon_lock() == lock_via_real


def test_root_is_canonical_not_merely_absolute(monkeypatch, tmp_path):
    """A traversal spelling ('<root>/x/..') is the same root. Guards the case where a caller
    builds NELIX_HOME by joining, which absolute-ness alone would not collapse."""
    real = tmp_path / "real"; real.mkdir()
    monkeypatch.setenv("NELIX_HOME", str(real / "sub" / ".."))
    (real / "sub").mkdir()
    importlib.reload(paths)
    assert paths.nelix_root() == real


# --- sun_path: the root is operator-settable now, so it can be made unbindable ---------

def test_rpc_sock_under_the_default_root_is_bindable(monkeypatch):
    monkeypatch.delenv("NELIX_HOME", raising=False)
    importlib.reload(paths)
    assert len(str(paths.rpc_sock()).encode()) < paths.SUN_PATH_MAX


def test_sun_path_overflow_flags_an_over_long_node_and_names_both_sources(monkeypatch, tmp_path):
    deep = tmp_path / ("d" * 120)
    monkeypatch.setenv("NELIX_HOME", str(deep))
    importlib.reload(paths)
    sock = paths.rpc_sock()                              # accessor stays pure: no raise
    problem = paths.sun_path_overflow(sock)
    assert problem is not None
    assert str(sock) in problem and "NELIX_HOME" in problem and "NELIX_RPC_SOCK" in problem


def test_sun_path_overflow_passes_a_node_that_fits():
    assert paths.sun_path_overflow("/tmp/nx.sock") is None


def test_sun_path_overflow_boundary_is_the_byte_the_kernel_rejects():
    """Off-by-one here is the whole guard: sun_path counts the NUL terminator."""
    fits = "/tmp/" + "s" * (paths.SUN_PATH_MAX - 1 - len("/tmp/"))
    assert len(fits.encode()) == paths.SUN_PATH_MAX - 1
    assert paths.sun_path_overflow(fits) is None
    assert paths.sun_path_overflow(fits + "s") is not None


def test_sun_path_limit_matches_what_the_kernel_actually_enforces(tmp_path):
    """SUN_PATH_MAX is a claim about this platform; measure it rather than trust the constant.
    Binds at the longest path we allow (must succeed) and at one byte more (must fail)."""
    import socket
    base = Path("/tmp")
    longest = paths.SUN_PATH_MAX - 1                     # what rpc_sock() permits
    for n, must_bind in ((longest, True), (longest + 1, False)):
        p = str(base / ("s" * (n - len(str(base)) - 1)))
        assert len(p.encode()) == n
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            try:
                s.bind(p)
                bound = True
            except OSError:
                bound = False
            assert bound is must_bind, f"len={n}: bind bound={bound}, expected {must_bind}"
        finally:
            s.close()
            try:
                os.unlink(p)
            except OSError:
                pass


def test_daemon_glob_matches_spawn_files_not_latest():
    assert fnmatch.fnmatch("daemon-20260624-170000-4242.log", paths.DAEMON_LOG_GLOB)
    assert not fnmatch.fnmatch("daemon-latest.log", paths.DAEMON_LOG_GLOB)


# --- 0700 / 0600 ----------------------------------------------------------------------

def test_ensure_private_dir_is_0700_down_to_nelix_root(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths)
    # MEASURED (2026-07-17): pytest creates tmp_path 0700 already. nelix_root() IS tmp_path at
    # the new location, so asserting 0700 on a root we never loosened would pass whether or not
    # ensure_private_dir touched it — vacuous exactly where the slice changed the behaviour
    # (the root used to be a subdir we created; it is the operator's named dir now). Loosen
    # first so 0700 can ONLY come from the code under test.
    os.chmod(tmp_path, 0o755)
    d = paths.sessions_root() / "s-abc"
    paths.ensure_private_dir(d)
    for level in (d, paths.sessions_root(), paths.nelix_root()):
        assert oct(level.stat().st_mode & 0o777) == "0o700", level


def test_ensure_private_dir_corrects_existing_loose_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path / "root"))
    importlib.reload(paths)
    root = paths.nelix_root(); root.mkdir(parents=True); os.chmod(root, 0o755)
    paths.ensure_private_dir(root)
    assert oct(root.stat().st_mode & 0o777) == "0o700"


def test_ensure_private_dir_leaves_ancestors_above_the_root_alone(monkeypatch, tmp_path):
    """The walk stops AT nelix_root. Whatever contains $NELIX_HOME (a home directory) is not
    ours to tighten — the old layout relied on this to leave a shared HERMES_HOME alone, and
    the property must survive the root moving up to $NELIX_HOME itself."""
    parent = tmp_path / "parent"; parent.mkdir(); os.chmod(parent, 0o755)
    monkeypatch.setenv("NELIX_HOME", str(parent / "nelix"))
    importlib.reload(paths)
    paths.ensure_private_dir(paths.sessions_root() / "s-abc")
    assert oct(paths.nelix_root().stat().st_mode & 0o777) == "0o700"
    assert oct(parent.stat().st_mode & 0o777) == "0o755", "tightened a dir above the root"


def test_private_opener_creates_0600(tmp_path):
    f = tmp_path / "secret"
    with open(f, "w", opener=paths.private_opener) as fh:
        fh.write("x")
    assert oct(f.stat().st_mode & 0o777) == "0o600"


def test_daemon_lock_and_child_record(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths)
    assert paths.daemon_lock() == paths.nelix_root() / "daemon.lock"
    sd = paths.sessions_root() / "s-12345678"
    assert paths.child_record(sd) == sd / "child.json"


def test_rpc_sock_lives_under_nelix_root(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths)
    assert paths.rpc_sock() == paths.nelix_root() / "rpc.sock"
    assert paths.rpc_sock().parent == paths.nelix_root()
