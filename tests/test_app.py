import io, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.obs import Logger
from daemon.app import warn_invalid_log_level
from daemon import app                       # noqa: E402
from daemon.transport import Transport       # noqa: E402
from daemon.config import LogLevelConfig
import paths                                 # noqa: E402


def test_invalid_log_level_warns_once():
    buf = io.StringIO()
    warn_invalid_log_level(Logger(level="info", stream=buf),
                           LogLevelConfig(level="info", invalid_value="loud", invalid_source="env"))
    recs = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
    assert len(recs) == 1 and recs[0]["event"] == "invalid_log_level"
    assert recs[0]["value"] == "loud" and recs[0]["source"] == "env" and recs[0]["using"] == "info"


def test_valid_log_level_no_warning():
    buf = io.StringIO()
    warn_invalid_log_level(Logger(stream=buf), LogLevelConfig(level="info"))
    assert buf.getvalue() == ""


def test_install_stack_dump_handler_enables_faulthandler():
    import faulthandler
    from daemon.app import install_stack_dump_handler
    install_stack_dump_handler()
    assert faulthandler.is_enabled()


def test_acquire_singleton_second_call_conflicts(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    import importlib, io, paths
    importlib.reload(paths)
    from daemon import app, singleton
    from daemon.obs import Logger
    buf = io.StringIO()
    fd = app.acquire_singleton(Logger(level="info", stream=buf))
    assert fd is not None
    holder = singleton.read_holder(paths.daemon_lock())
    assert holder["pid"] == __import__("os").getpid()
    buf2 = io.StringIO()
    assert app.acquire_singleton(Logger(level="info", stream=buf2)) is None
    assert "daemon_lock_conflict" in buf2.getvalue()
    __import__("os").close(fd)


def test_build_reaper_ctx_has_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    from daemon import app
    import os
    ctx = app.build_reaper_ctx(grace=3.0)
    assert ctx.daemon_pid == os.getpid()
    assert isinstance(ctx.daemon_fingerprint, str) and ctx.daemon_fingerprint
    assert ctx.grace == 3.0


def test_load_specs_skips_bad_keeps_good_and_logs(tmp_path):
    import io, json
    from daemon.app import load_specs
    from daemon.obs import Logger
    cfg = tmp_path / "n.toml"
    cfg.write_text('[executors.good]\ncommand="g"\ndriver="claude"\n'
                   '[executors.bad]\ncommand="b"\n')          # missing driver
    buf = io.StringIO()
    specs = load_specs(str(cfg), Logger(level="info", stream=buf))
    assert set(specs) == {"good"}
    recs = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
    assert any(r["event"] == "executor_skipped" and r.get("executor") == "bad" for r in recs)


def test_load_specs_parse_error_returns_empty_and_logs(tmp_path):
    import io, json
    from daemon.app import load_specs
    from daemon.obs import Logger
    cfg = tmp_path / "n.toml"
    cfg.write_text('[oops')
    buf = io.StringIO()
    specs = load_specs(str(cfg), Logger(level="info", stream=buf))
    assert specs == {}
    recs = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
    assert any(r["event"] == "config_parse_error" for r in recs)


def test_transport_from_env_defaults_to_unix(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    monkeypatch.delenv("NELIX_RPC_TRANSPORT", raising=False)
    import importlib, paths
    importlib.reload(paths)
    t = app.transport_from_env()
    assert t.kind == "unix" and t.path.endswith("/rpc.sock")


def test_transport_from_env_tcp(monkeypatch):
    monkeypatch.setenv("NELIX_RPC_TRANSPORT", "tcp")
    monkeypatch.setenv("NELIX_RPC_HOST", "127.0.0.1")
    monkeypatch.setenv("NELIX_RPC_PORT", "51000")
    monkeypatch.setenv("NELIX_RPC_TOKEN", "abc")
    assert app.transport_from_env() == Transport.tcp("127.0.0.1", 51000, "abc")


def test_explicit_rpc_sock_does_not_derive_the_nelix_home_node(monkeypatch, tmp_path):
    """An explicit NELIX_RPC_SOCK must WIN without the $NELIX_HOME node being derived at all.

    `dict.get(k, default)` evaluates its default eagerly, so the old spelling built the derived
    path even when it was about to be thrown away. Harmless while rpc_sock() was total — a trap
    the moment anything about the derived path can fail. Proven by making the derivation blow up:
    if it is still evaluated, this test raises instead of passing.
    """
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    monkeypatch.delenv("NELIX_RPC_TRANSPORT", raising=False)
    monkeypatch.setenv("NELIX_RPC_SOCK", "/tmp/nx-explicit.sock")
    boom = lambda: (_ for _ in ()).throw(AssertionError("derived the NELIX_HOME node anyway"))
    monkeypatch.setattr(paths, "rpc_sock", boom)
    assert app.transport_from_env() == Transport.unix("/tmp/nx-explicit.sock")
