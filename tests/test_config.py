from daemon.config import load_executors


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
