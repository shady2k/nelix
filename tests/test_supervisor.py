import importlib
import json
import os
import sys
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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
    state = tmp_path / "nelix" / ".active.json"
    assert state.exists()
    assert oct(state.stat().st_mode & 0o777) == "0o600"
    data = json.loads(state.read_text())
    assert data["token"] == token and data["pid"] > 0
    supervisor.teardown()


def test_ensure_running_reuses_live_daemon(monkeypatch, tmp_path):
    _use_fake_daemon(monkeypatch, tmp_path)
    base1, tok1 = supervisor.ensure_running()
    pid1 = json.loads((tmp_path / "nelix" / ".active.json").read_text())["pid"]
    base2, tok2 = supervisor.ensure_running()
    pid2 = json.loads((tmp_path / "nelix" / ".active.json").read_text())["pid"]
    assert (base1, tok1) == (base2, tok2) and pid1 == pid2  # no respawn
    supervisor.teardown()


def test_teardown_kills_and_clears(monkeypatch, tmp_path):
    _use_fake_daemon(monkeypatch, tmp_path)
    supervisor.ensure_running()
    pid = json.loads((tmp_path / "nelix" / ".active.json").read_text())["pid"]
    supervisor.teardown("test")
    assert not (tmp_path / "nelix" / ".active.json").exists()
    time.sleep(0.3)
    with __import__("pytest").raises(OSError):
        os.kill(pid, 0)  # process gone


def test_stale_state_triggers_respawn(monkeypatch, tmp_path):
    _use_fake_daemon(monkeypatch, tmp_path)
    state = tmp_path / "nelix" / ".active.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"pid": 999999, "port": 1, "token": "dead"}))
    base, token = supervisor.ensure_running()  # dead pid -> respawn
    assert token != "dead"
    supervisor.teardown()


def test_ensure_deps_installs_venv_scoped_when_missing(monkeypatch):
    importlib.reload(supervisor)
    calls = []
    # missing before install, present after (calls is non-empty post-run)
    monkeypatch.setattr(supervisor, "_deps_present", lambda: bool(calls))
    monkeypatch.setattr(supervisor, "_lazy_installs_allowed", lambda: True)

    def fake_run(cmd, **k):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(supervisor.subprocess, "run", fake_run)
    supervisor._ensure_deps()
    assert calls and calls[0][:3] == [sys.executable, "-m", "pip"]
    assert "install" in calls[0]
    for pkg in ("pyte", "ptyprocess"):
        assert any(pkg in str(a) for a in calls[0])


def test_ensure_deps_raises_when_lazy_installs_disabled(monkeypatch):
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor, "_deps_present", lambda: False)
    monkeypatch.setattr(supervisor, "_lazy_installs_allowed", lambda: False)
    import pytest as _pt
    with _pt.raises(RuntimeError):
        supervisor._ensure_deps()
