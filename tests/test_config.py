import pytest

from daemon.config import load_executors, load_concurrency_limit


def test_load_executor_spec(tmp_path):
    cfg = tmp_path / "nelix.toml"
    cfg.write_text(
        '[executors.demo]\n'
        'command = "tool"\n'
        'args = ["-x", "--", "~/w.sh"]\n'
        'env = {FOO = "bar"}\n'
        'cwd = "~/work"\n'
        'driver = "claude"\n'
    )
    spec = load_executors(str(cfg))["demo"]
    assert spec.argv() == ["tool", "-x", "--", "~/w.sh"]
    assert spec.driver == "claude"
    assert spec.resolved_env()["FOO"] == "bar"
    # cwd is per-session (a nelix_start arg), not a config field — any config cwd is ignored.
    assert not hasattr(spec, "cwd")


def test_launcher_defaults_to_auto(tmp_path):
    cfg = tmp_path / "n.toml"
    cfg.write_text('[executors.demo]\ncommand="tool"\ndriver="claude"\n')
    spec = load_executors(str(cfg))["demo"]
    assert spec.launcher == "auto"


def test_missing_driver_raises(tmp_path):
    cfg = tmp_path / "n.toml"
    cfg.write_text('[executors.demo]\ncommand="tool"\n')
    with pytest.raises(ValueError, match="demo"):
        load_executors(str(cfg))


def test_capture_tunables_have_defaults_and_overrides(tmp_path):
    from daemon.config import load_executors
    cfg = tmp_path / "n.toml"
    cfg.write_text(
        '[executors.a]\ncommand="x"\nargs=[]\nenv={}\ncwd="."\ndriver="claude"\nlauncher="local"\n'
        '[executors.b]\ncommand="y"\nargs=[]\nenv={}\ncwd="."\ndriver="claude"\nlauncher="local"\n'
        'settle_seconds=3.0\nmax_idle_seconds=120\ntail_lines=50\nstatus_tail_chars=1000\n'
        'dialog_page_chars=2000\nspool_max_bytes=4096\n')
    specs = load_executors(str(cfg))
    a, b = specs["a"], specs["b"]
    assert (a.settle_seconds, a.max_idle_seconds, a.tail_lines) == (1.5, 600.0, 400)
    assert a.status_tail_chars == 4000 and a.dialog_page_chars == 8000 and a.spool_max_bytes == 8_388_608
    assert (b.settle_seconds, b.max_idle_seconds, b.tail_lines) == (3.0, 120.0, 50)
    assert b.status_tail_chars == 1000 and b.dialog_page_chars == 2000 and b.spool_max_bytes == 4096


def test_recovery_thresholds_defaults_and_overrides(tmp_path):
    cfg = tmp_path / "n.toml"
    cfg.write_text(
        '[executors.a]\ncommand="x"\ndriver="claude"\n'
        '[executors.b]\ncommand="y"\ndriver="claude"\n'
        'max_idle_seconds=120\nmax_restarts=5\n')
    specs = load_executors(str(cfg))
    a, b = specs["a"], specs["b"]
    assert (a.max_idle_seconds, a.max_restarts) == (600.0, 3)
    assert (b.max_idle_seconds, b.max_restarts) == (120.0, 5)
    assert not hasattr(a, "hang_timeout")          # renamed, not kept alongside
    assert not hasattr(a, "max_runtime_seconds")   # dropped: no time-ceiling logic in the daemon


def test_recovery_thresholds_reject_bad_values(tmp_path):
    cfg = tmp_path / "n.toml"
    # non-numeric / negative / bool must fall back to the default, not crash the load.
    cfg.write_text('[executors.a]\ncommand="x"\ndriver="claude"\n'
                   'max_idle_seconds="oops"\nmax_restarts=true\n')
    a = load_executors(str(cfg))["a"]
    assert (a.max_idle_seconds, a.max_restarts) == (600.0, 3)


def test_concurrency_limit(tmp_path):
    cfg = tmp_path / "n.toml"
    cfg.write_text('concurrency_limit = 3\n[executors.demo]\ncommand="t"\n')
    assert load_concurrency_limit(str(cfg)) == 3
    cfg2 = tmp_path / "n2.toml"
    cfg2.write_text('[executors.demo]\ncommand="t"\n')
    assert load_concurrency_limit(str(cfg2)) == 1


def test_delivery_confirm_seconds_loads_with_default(tmp_path):
    from daemon.config import load_executors
    p = tmp_path / "nelix.toml"
    p.write_text(
        '[executors.a]\ncommand="x"\ndriver="claude"\n'
        '[executors.b]\ncommand="y"\ndriver="claude"\ndelivery_confirm_seconds=3.5\n')
    specs = load_executors(str(p))
    assert specs["a"].delivery_confirm_seconds == 10.0      # default
    assert specs["b"].delivery_confirm_seconds == 3.5       # overridden


def test_load_retention_defaults_when_missing(tmp_path):
    from daemon.config import load_retention
    r = load_retention(str(tmp_path / "absent.toml"))
    assert (r.daemon_log_retain, r.session_retain, r.session_max_age_days) == (10, 20, 7)


def test_load_retention_reads_values(tmp_path):
    from daemon.config import load_retention
    cfg = tmp_path / "n.toml"
    cfg.write_text("daemon_log_retain = 3\nsession_retain = 5\nsession_max_age_days = 2\n"
                   '[executors.demo]\ncommand="t"\ndriver="claude"\n')
    r = load_retention(str(cfg))
    assert (r.daemon_log_retain, r.session_retain, r.session_max_age_days) == (3, 5, 2)


def test_load_retention_validates(tmp_path):
    from daemon.config import load_retention
    cfg = tmp_path / "n.toml"
    # log retain floor is 1 (0/neg -> default); session rakes allow 0 (disabled); neg/non-int -> default
    cfg.write_text('daemon_log_retain = 0\nsession_retain = 0\nsession_max_age_days = -4\n')
    r = load_retention(str(cfg))
    assert r.daemon_log_retain == 10      # 0 < floor 1 -> default
    assert r.session_retain == 0          # 0 is valid (disabled)
    assert r.session_max_age_days == 7    # negative -> default


def test_load_retention_rejects_float_and_bool(tmp_path):
    from daemon.config import load_retention
    cfg = tmp_path / "n.toml"
    # non-int TOML values (float, bool) must fall back to the default, not coerce via int().
    cfg.write_text("daemon_log_retain = 1.9\nsession_retain = true\nsession_max_age_days = 2.0\n")
    r = load_retention(str(cfg))
    assert (r.daemon_log_retain, r.session_retain, r.session_max_age_days) == (10, 20, 7)


def test_load_log_level_default_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("NELIX_LOG_LEVEL", raising=False)
    from daemon.config import load_log_level
    p = tmp_path / "nelix.toml"; p.write_text("concurrency_limit = 1\n")
    cfg = load_log_level(str(p))
    assert cfg.level == "info" and cfg.invalid_value is None


def test_load_log_level_from_file_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.delenv("NELIX_LOG_LEVEL", raising=False)
    from daemon.config import load_log_level
    p = tmp_path / "nelix.toml"; p.write_text('log_level = "DEBUG"\n')
    cfg = load_log_level(str(p))
    assert cfg.level == "debug" and cfg.invalid_value is None


def test_load_log_level_invalid_file_falls_back_and_flags(tmp_path, monkeypatch):
    monkeypatch.delenv("NELIX_LOG_LEVEL", raising=False)
    from daemon.config import load_log_level
    p = tmp_path / "nelix.toml"; p.write_text('log_level = "verbose"\n')
    cfg = load_log_level(str(p))
    assert cfg.level == "info" and cfg.invalid_value == "verbose" and cfg.invalid_source == "file"


def test_env_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NELIX_LOG_LEVEL", "warning")
    from daemon.config import load_log_level
    p = tmp_path / "nelix.toml"; p.write_text('log_level = "debug"\n')
    cfg = load_log_level(str(p))
    assert cfg.level == "warning" and cfg.invalid_value is None


def test_invalid_env_falls_back_to_valid_file_but_flags_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NELIX_LOG_LEVEL", "loud")
    from daemon.config import load_log_level
    p = tmp_path / "nelix.toml"; p.write_text('log_level = "debug"\n')
    cfg = load_log_level(str(p))
    assert cfg.level == "debug" and cfg.invalid_value == "loud" and cfg.invalid_source == "env"
