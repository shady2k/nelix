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
