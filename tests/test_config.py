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
    assert not spec.resolved_cwd().startswith("~")


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
        'settle_seconds=3.0\nhang_timeout=120\ntail_lines=50\nstatus_tail_chars=1000\n'
        'dialog_page_chars=2000\nspool_max_bytes=4096\n')
    specs = load_executors(str(cfg))
    a, b = specs["a"], specs["b"]
    assert (a.settle_seconds, a.hang_timeout, a.tail_lines) == (1.5, 600.0, 400)
    assert a.status_tail_chars == 4000 and a.dialog_page_chars == 8000 and a.spool_max_bytes == 8_388_608
    assert (b.settle_seconds, b.hang_timeout, b.tail_lines) == (3.0, 120.0, 50)
    assert b.status_tail_chars == 1000 and b.dialog_page_chars == 2000 and b.spool_max_bytes == 4096


def test_concurrency_limit(tmp_path):
    cfg = tmp_path / "n.toml"
    cfg.write_text('concurrency_limit = 3\n[executors.demo]\ncommand="t"\n')
    assert load_concurrency_limit(str(cfg)) == 3
    cfg2 = tmp_path / "n2.toml"
    cfg2.write_text('[executors.demo]\ncommand="t"\n')
    assert load_concurrency_limit(str(cfg2)) == 1


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
