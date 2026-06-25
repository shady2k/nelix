import fnmatch
import importlib
import os

import paths


def test_layout_all_under_workspace_nelix(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    importlib.reload(paths)
    root = tmp_path / "workspace" / "nelix"
    assert paths.hermes_home() == tmp_path
    assert paths.nelix_root() == root
    assert paths.config_path() == root / "nelix.toml"
    assert paths.state_file() == root / ".active.json"
    assert paths.sessions_root() == root / "sessions"
    assert paths.logs_dir() == root / "logs"
    assert paths.daemon_log("20260624-170000", 4242) == root / "logs" / "daemon-20260624-170000-4242.log"
    assert paths.daemon_latest() == root / "logs" / "daemon-latest.log"


def test_daemon_glob_matches_spawn_files_not_latest():
    assert fnmatch.fnmatch("daemon-20260624-170000-4242.log", paths.DAEMON_LOG_GLOB)
    assert not fnmatch.fnmatch("daemon-latest.log", paths.DAEMON_LOG_GLOB)


def test_ensure_private_dir_is_0700_down_to_nelix_root(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    importlib.reload(paths)
    d = paths.sessions_root() / "s-abc"
    paths.ensure_private_dir(d)
    for level in (d, paths.sessions_root(), paths.nelix_root()):
        assert oct(level.stat().st_mode & 0o777) == "0o700", level


def test_ensure_private_dir_corrects_existing_loose_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    importlib.reload(paths)
    root = paths.nelix_root(); root.mkdir(parents=True); os.chmod(root, 0o755)
    paths.ensure_private_dir(root)
    assert oct(root.stat().st_mode & 0o777) == "0o700"


def test_private_opener_creates_0600(tmp_path):
    f = tmp_path / "secret"
    with open(f, "w", opener=paths.private_opener) as fh:
        fh.write("x")
    assert oct(f.stat().st_mode & 0o777) == "0o600"
