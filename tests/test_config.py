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


def test_concurrency_limit(tmp_path):
    cfg = tmp_path / "n.toml"
    cfg.write_text('concurrency_limit = 3\n[executors.demo]\ncommand="t"\n')
    assert load_concurrency_limit(str(cfg)) == 3
    cfg2 = tmp_path / "n2.toml"
    cfg2.write_text('[executors.demo]\ncommand="t"\n')
    assert load_concurrency_limit(str(cfg2)) == 1
