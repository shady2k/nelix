import importlib
import json
import os
import sys
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402
import supervisor  # noqa: E402

# A fake daemon: serves /status 200 iff the token header matches. Honors
# NELIX_RPC_TOKEN / NELIX_RPC_PORT exactly like the real daemon entry.
_FAKE = textwrap.dedent("""
    import os, json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    tok = os.environ["NELIX_RPC_TOKEN"]; port = int(os.environ["NELIX_RPC_PORT"])
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.headers.get("X-Nelix-Token") == tok
            self.send_response(200 if ok else 401)
            self.send_header("Content-Length","2"); self.end_headers(); self.wfile.write(b"{}")
        def log_message(self,*a): pass
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
""")


def _use_fake_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake = tmp_path / "fake_daemon.py"
    fake.write_text(_FAKE)
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor, "_daemon_argv", lambda: [sys.executable, str(fake)])


def test_ensure_running_spawns_and_writes_state(monkeypatch, tmp_path):
    _use_fake_daemon(monkeypatch, tmp_path)
    base, token = supervisor.ensure_running()
    assert base.startswith("http://127.0.0.1:")
    state = paths.state_file()
    assert state.exists()
    assert oct(state.stat().st_mode & 0o777) == "0o600"
    data = json.loads(state.read_text())
    assert data["token"] == token and data["pid"] > 0
    supervisor.teardown()


def test_ensure_running_reuses_live_daemon(monkeypatch, tmp_path):
    _use_fake_daemon(monkeypatch, tmp_path)
    base1, tok1 = supervisor.ensure_running()
    pid1 = json.loads(paths.state_file().read_text())["pid"]
    base2, tok2 = supervisor.ensure_running()
    pid2 = json.loads(paths.state_file().read_text())["pid"]
    assert (base1, tok1) == (base2, tok2) and pid1 == pid2  # no respawn
    supervisor.teardown()


def test_teardown_kills_and_clears(monkeypatch, tmp_path):
    _use_fake_daemon(monkeypatch, tmp_path)
    supervisor.ensure_running()
    pid = json.loads(paths.state_file().read_text())["pid"]
    supervisor.teardown("test")
    assert not paths.state_file().exists()
    time.sleep(0.3)
    with __import__("pytest").raises(OSError):
        os.kill(pid, 0)  # process gone


def test_stale_state_triggers_respawn(monkeypatch, tmp_path):
    _use_fake_daemon(monkeypatch, tmp_path)
    state = paths.state_file()
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"pid": 999999, "port": 1, "token": "dead"}))
    base, token = supervisor.ensure_running()  # dead pid -> respawn
    assert token != "dead"
    supervisor.teardown()


def test_ensure_deps_calls_installer_when_missing(monkeypatch):
    importlib.reload(supervisor)
    calls = []
    # missing before install, present after (calls non-empty post-run)
    monkeypatch.setattr(supervisor, "_deps_present", lambda: bool(calls))
    monkeypatch.setattr(supervisor, "_lazy_installs_allowed", lambda: True)
    monkeypatch.setattr(supervisor, "_venv_pip_install",
                        lambda specs: (calls.append(tuple(specs)) or (True, "")))
    supervisor._ensure_deps()
    assert calls == [supervisor._DAEMON_DEPS]


def test_ensure_deps_raises_when_lazy_installs_disabled(monkeypatch):
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor, "_deps_present", lambda: False)
    monkeypatch.setattr(supervisor, "_lazy_installs_allowed", lambda: False)
    import pytest as _pt
    with _pt.raises(RuntimeError):
        supervisor._ensure_deps()


def test_ensure_deps_raises_when_install_fails(monkeypatch):
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor, "_deps_present", lambda: False)  # never becomes present
    monkeypatch.setattr(supervisor, "_lazy_installs_allowed", lambda: True)
    monkeypatch.setattr(supervisor, "_venv_pip_install", lambda specs: (False, "boom"))
    import pytest as _pt
    with _pt.raises(RuntimeError):
        supervisor._ensure_deps()


def _record_run(record, rc_for):
    def fake(cmd, **k):
        record.append(cmd)
        class R:
            returncode = rc_for(cmd)
            stdout = ""
            stderr = ""
        return R()
    return fake


def test_venv_pip_install_prefers_uv(monkeypatch):
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor.shutil, "which",
                        lambda b: "/usr/bin/uv" if b == "uv" else None)
    record = []
    monkeypatch.setattr(supervisor.subprocess, "run", _record_run(record, lambda cmd: 0))
    ok, _out = supervisor._venv_pip_install(("pyte==0.8.2",))
    assert ok is True
    assert record[0][0] == "/usr/bin/uv" and record[0][1:3] == ["pip", "install"]
    assert len(record) == 1  # uv succeeded -> no pip fallback


def test_venv_pip_install_falls_through_to_pip_when_uv_fails(monkeypatch):
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor.shutil, "which",
                        lambda b: "/usr/bin/uv" if b == "uv" else None)
    record = []
    # uv present but exits non-zero -> must fall through to the pip tier
    monkeypatch.setattr(supervisor.subprocess, "run",
                        _record_run(record, lambda cmd: 1 if cmd[0] == "/usr/bin/uv" else 0))
    ok, _out = supervisor._venv_pip_install(("pyte==0.8.2",))
    assert ok is True
    assert record[0][0] == "/usr/bin/uv"  # uv tried first
    assert any(c[:3] == [sys.executable, "-m", "pip"] and "install" in c for c in record)


def test_venv_pip_install_falls_back_to_pip_when_no_uv(monkeypatch):
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor.shutil, "which", lambda b: None)
    record = []
    monkeypatch.setattr(supervisor.subprocess, "run", _record_run(record, lambda cmd: 0))
    ok, _out = supervisor._venv_pip_install(("pyte==0.8.2",))
    assert ok is True
    assert [sys.executable, "-m", "pip", "--version"] in record
    assert any(c[:3] == [sys.executable, "-m", "pip"] and "install" in c for c in record)


def test_venv_pip_install_bootstraps_ensurepip_when_pip_missing(monkeypatch):
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor.shutil, "which", lambda b: None)
    record = []
    monkeypatch.setattr(supervisor.subprocess, "run",
                        _record_run(record, lambda cmd: 1 if "--version" in cmd else 0))
    ok, _out = supervisor._venv_pip_install(("pyte==0.8.2",))
    assert ok is True
    assert any("ensurepip" in c for c in record)


def test_daemon_log_is_per_spawn_named_with_pid(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    importlib.reload(supervisor)
    root = supervisor._root(); root.mkdir(parents=True)
    p = supervisor._open_daemon_log(root)
    assert p.parent == root
    assert p.name.startswith("daemon-") and p.name.endswith(f"-{os.getpid()}.log")
    assert (root / "daemon-latest.log").is_symlink()
    assert (root / "daemon-latest.log").resolve() == p.resolve()


def test_prune_keeps_newest_retain_and_spares_symlink(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    importlib.reload(supervisor)
    root = supervisor._root(); root.mkdir(parents=True)
    made = []
    for i in range(5):
        f = root / f"daemon-2026010{i}-000000-{1000+i}.log"
        f.write_text("x"); os.utime(f, (i, i))   # ascending mtime
        made.append(f)
    (root / "daemon-latest.log").symlink_to(made[-1].name)
    supervisor._prune_daemon_logs(root, retain=2)
    survivors = sorted(p.name for p in root.glob("daemon-*-*.log") if not p.is_symlink())
    assert survivors == [made[3].name, made[4].name]   # newest 2 kept
    assert (root / "daemon-latest.log").is_symlink()    # symlink untouched
