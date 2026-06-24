import fnmatch
import importlib

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
    assert paths.daemon_log("20260624-170000", 4242) == root / "daemon-20260624-170000-4242.log"
    assert paths.daemon_latest() == root / "daemon-latest.log"


def test_daemon_glob_matches_spawn_files_not_latest():
    assert fnmatch.fnmatch("daemon-20260624-170000-4242.log", paths.DAEMON_LOG_GLOB)
    assert not fnmatch.fnmatch("daemon-latest.log", paths.DAEMON_LOG_GLOB)
