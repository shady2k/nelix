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


def _write_cfg(tmp_path, body):
    cfg = tmp_path / "workspace" / "nelix" / "nelix.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(body)
    return cfg


def test_validate_clean_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import registry
    _write_cfg(tmp_path, '[executors.good]\ncommand="g"\ndriver="claude"\n')
    v = registry.validate()
    assert v["parse_error"] is None and v["executor_errors"] == []


def test_validate_collects_bad_executor(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import registry
    _write_cfg(tmp_path, '[executors.good]\ncommand="g"\ndriver="claude"\n'
                         '[executors.bad]\ncommand="b"\n')
    v = registry.validate()
    assert v["parse_error"] is None
    assert [e["name"] for e in v["executor_errors"]] == ["bad"]


def test_validate_parse_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import registry
    _write_cfg(tmp_path, '[oops')
    v = registry.validate()
    assert v["parse_error"]


def test_config_error_for_parse_error():
    import registry
    v = {"parse_error": "could not parse /x: bad", "executor_errors": []}
    err = registry.config_error_for(v, "anything")
    assert err and "config" in err["error"].lower()
    assert err["config_errors"] == [{"name": None, "problem": "could not parse /x: bad"}]


def test_config_error_for_disabled_executor():
    import registry
    v = {"parse_error": None,
         "executor_errors": [{"name": "bad", "problem": "executor 'bad': 'driver' is required"}]}
    err = registry.config_error_for(v, "bad")
    assert err and "bad" in err["error"] and "config" in err["error"].lower()
    assert err["config_errors"] == v["executor_errors"]


def test_config_error_for_valid_or_unknown_returns_none():
    import registry
    v = {"parse_error": None, "executor_errors": [{"name": "bad", "problem": "x"}]}
    assert registry.config_error_for(v, "good") is None      # valid
    assert registry.config_error_for(v, "typo") is None      # genuinely unknown -> daemon answers
