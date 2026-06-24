import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402
import registry  # noqa: E402


def test_hermes_home_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    importlib.reload(paths)
    importlib.reload(registry)
    assert paths.hermes_home() == tmp_path
    assert registry.config_path() == tmp_path / "workspace" / "nelix" / "nelix.toml"


def test_names_reads_executor_table(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = paths.config_path()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        '[executors.opencode]\ncommand="opencode"\nargs=[]\nenv={}\ncwd="."\n'
        'driver="claude"\nlauncher="local"\n'
        '[executors.codex]\ncommand="codex"\nargs=[]\nenv={}\ncwd="."\n'
        'driver="claude"\nlauncher="local"\n')
    assert registry.names() == ["codex", "opencode"]


def test_names_empty_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert registry.names() == []


def test_seed_if_absent_copies_example(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert registry.seed_if_absent() is True
    assert registry.config_path().exists()
    assert registry.seed_if_absent() is False  # idempotent
