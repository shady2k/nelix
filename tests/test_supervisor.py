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
from daemon.transport import Transport  # noqa: E402

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
    monkeypatch.setenv("NELIX_RPC_TRANSPORT", "tcp")
    fake = tmp_path / "fake_daemon.py"
    fake.write_text(_FAKE)
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor, "_daemon_argv", lambda: [sys.executable, str(fake)])


def test_ensure_running_spawns_and_writes_state(monkeypatch, tmp_path):
    _use_fake_daemon(monkeypatch, tmp_path)
    transport = supervisor.ensure_running()
    assert transport.kind == "tcp"
    assert transport.host == "127.0.0.1"
    state = paths.state_file()
    assert state.exists()
    assert oct(state.stat().st_mode & 0o777) == "0o600"
    data = json.loads(state.read_text())
    assert data["token"] == transport.token and data["pid"] > 0
    supervisor.teardown()


def test_ensure_running_reuses_live_daemon(monkeypatch, tmp_path):
    _use_fake_daemon(monkeypatch, tmp_path)
    t1 = supervisor.ensure_running()
    pid1 = json.loads(paths.state_file().read_text())["pid"]
    t2 = supervisor.ensure_running()
    pid2 = json.loads(paths.state_file().read_text())["pid"]
    assert t1 == t2 and pid1 == pid2  # no respawn
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
    state.write_text(json.dumps({"pid": 999999, "transport": "tcp",
                                 "host": "127.0.0.1", "port": 1, "token": "dead"}))
    transport = supervisor.ensure_running()  # dead pid -> respawn
    assert transport.token != "dead"
    supervisor.teardown()


def test_ensure_deps_installs_from_hash_lock_when_missing(monkeypatch):
    importlib.reload(supervisor)
    calls = []
    # missing before install, present after (calls non-empty post-run)
    monkeypatch.setattr(supervisor, "_deps_present", lambda: bool(calls))
    monkeypatch.setattr(supervisor, "_lazy_installs_allowed", lambda: True)
    monkeypatch.setattr(supervisor, "_venv_pip_install",
                        lambda req: (calls.append(req) or (True, "")))
    supervisor._ensure_deps()
    assert calls == [supervisor._DAEMON_LOCK]      # installs from the hash-pinned lock, not bare specs


def test_deps_present_requires_exact_version(monkeypatch):
    importlib.reload(supervisor)
    versions = {"pyte": "0.8.2", "ptyprocess": "0.7.0"}
    monkeypatch.setattr(supervisor.importlib.metadata, "version", lambda n: versions[n])
    assert supervisor._deps_present() is True
    versions["pyte"] = "0.8.1"                     # a wrong version present must NOT count as ok
    assert supervisor._deps_present() is False


def test_deps_present_false_when_distribution_missing(monkeypatch):
    importlib.reload(supervisor)

    def boom(name):
        raise supervisor.importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(supervisor.importlib.metadata, "version", boom)
    assert supervisor._deps_present() is False


def test_deps_present_false_when_module_files_gone(monkeypatch):
    # exact metadata present but the module is not importable (corrupted install) -> reinstall
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor.importlib.metadata, "version",
                        lambda n: {"pyte": "0.8.2", "ptyprocess": "0.7.0"}[n])
    monkeypatch.setattr(supervisor.importlib.util, "find_spec", lambda m: None)
    assert supervisor._deps_present() is False


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


def test_venv_pip_install_prefers_uv_with_require_hashes(monkeypatch):
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor.shutil, "which",
                        lambda b: "/usr/bin/uv" if b == "uv" else None)
    record = []
    monkeypatch.setattr(supervisor.subprocess, "run", _record_run(record, lambda cmd: 0))
    ok, _out = supervisor._venv_pip_install("/lock")
    assert ok is True
    assert record[0][0] == "/usr/bin/uv" and record[0][1:3] == ["pip", "install"]
    assert "--require-hashes" in record[0] and record[0][-2:] == ["-r", "/lock"]
    assert len(record) == 1  # uv succeeded -> no pip fallback


def test_venv_pip_install_falls_through_to_pip_when_uv_fails(monkeypatch):
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor.shutil, "which",
                        lambda b: "/usr/bin/uv" if b == "uv" else None)
    record = []
    # uv present but exits non-zero -> must fall through to the pip tier
    monkeypatch.setattr(supervisor.subprocess, "run",
                        _record_run(record, lambda cmd: 1 if cmd[0] == "/usr/bin/uv" else 0))
    ok, _out = supervisor._venv_pip_install("/lock")
    assert ok is True
    assert record[0][0] == "/usr/bin/uv"  # uv tried first
    assert any(c[:3] == [sys.executable, "-m", "pip"] and "--require-hashes" in c for c in record)


def test_venv_pip_install_falls_back_to_pip_when_no_uv(monkeypatch):
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor.shutil, "which", lambda b: None)
    record = []
    monkeypatch.setattr(supervisor.subprocess, "run", _record_run(record, lambda cmd: 0))
    ok, _out = supervisor._venv_pip_install("/lock")
    assert ok is True
    assert [sys.executable, "-m", "pip", "--version"] in record
    assert any(c[:3] == [sys.executable, "-m", "pip"] and "--require-hashes" in c for c in record)


def test_venv_pip_install_bootstraps_ensurepip_when_pip_missing(monkeypatch):
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor.shutil, "which", lambda b: None)
    record = []
    monkeypatch.setattr(supervisor.subprocess, "run",
                        _record_run(record, lambda cmd: 1 if "--version" in cmd else 0))
    ok, _out = supervisor._venv_pip_install("/lock")
    assert ok is True
    assert any("ensurepip" in c for c in record)


def test_disable_uv_env_forces_pip_tier(monkeypatch):
    importlib.reload(supervisor)
    monkeypatch.setenv("NELIX_DISABLE_UV", "1")
    monkeypatch.setattr(supervisor.shutil, "which",
                        lambda b: "/usr/bin/uv" if b == "uv" else None)
    record = []
    monkeypatch.setattr(supervisor.subprocess, "run", _record_run(record, lambda cmd: 0))
    ok, _out = supervisor._venv_pip_install("/lock")
    assert ok is True
    assert all(c[0] != "/usr/bin/uv" for c in record)   # uv skipped despite being on PATH
    assert any(c[:3] == [sys.executable, "-m", "pip"] for c in record)


def test_daemon_log_is_per_spawn_named_under_logs_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    importlib.reload(supervisor)
    root = supervisor._root(); root.mkdir(parents=True)
    p = supervisor._open_daemon_log(root)
    logs = paths.logs_dir()
    assert p.parent == logs                                   # logs now live under logs/, not root
    assert p.name.startswith("daemon-") and p.name.endswith(f"-{os.getpid()}.log")
    assert oct(p.stat().st_mode & 0o777) == "0o600"           # log file is private
    assert oct(logs.stat().st_mode & 0o777) == "0o700"        # logs dir is private
    assert (logs / "daemon-latest.log").is_symlink()
    assert (logs / "daemon-latest.log").resolve() == p.resolve()


def test_open_daemon_log_migrates_legacy_root_logs(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    importlib.reload(supervisor)
    root = supervisor._root(); root.mkdir(parents=True)
    old = root / "daemon-20250101-000000-111.log"; old.write_text("old")   # legacy root layout
    (root / "daemon-latest.log").symlink_to(old.name)
    p = supervisor._open_daemon_log(root)
    logs = paths.logs_dir()
    assert (logs / old.name).read_text() == "old"             # legacy per-spawn log moved into logs/
    assert not old.exists()                                   # ... and removed from root
    assert not (root / "daemon-latest.log").exists()          # stale root symlink dropped
    assert p.parent == logs and (logs / "daemon-latest.log").is_symlink()


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


def test_teardown_logs_to_nelix_logger(monkeypatch, tmp_path, caplog):
    _use_fake_daemon(monkeypatch, tmp_path)
    supervisor.ensure_running()
    with caplog.at_level("INFO", logger="nelix.supervisor"):
        supervisor.teardown("unit test")
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "unit test" in msgs


def test_prune_spares_current_file_and_keeps_total_retain(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    importlib.reload(supervisor)
    root = supervisor._root(); root.mkdir(parents=True)
    # 3 old files (one FUTURE-dated so it sorts newest) + a "current" file with an OLDER mtime.
    # current must survive regardless of mtime, and the total must be exactly retain=2.
    for i, mt in enumerate((100, 5000, 200)):           # index 1 is future-dated
        f = root / f"daemon-2026010{i}-000000-{900 + i}.log"
        f.write_text("x"); os.utime(f, (mt, mt))
    current = root / f"daemon-20260109-000000-{os.getpid()}.log"
    current.write_text("c"); os.utime(current, (300, 300))   # NOT the newest by mtime
    supervisor._prune_daemon_logs(root, retain=2, keep=current)
    remaining = {p.name for p in root.glob("daemon-*-*.log")}
    assert current.name in remaining                    # current spared despite older mtime
    assert len(remaining) == 2                            # exactly retain total


def test_ensure_running_reuses_race_winner(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib, supervisor
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor, "_ensure_deps", lambda: None)
    monkeypatch.setattr(supervisor, "_open_daemon_log", lambda root: tmp_path / "d.log")

    # endpoint: first call (top of ensure_running) None; later (after our spawn "loses") -> winner
    calls = {"n": 0}
    def fake_endpoint():
        calls["n"] += 1
        return Transport.tcp("127.0.0.1", 8765, "winner-token") if calls["n"] >= 2 else None
    monkeypatch.setattr(supervisor, "endpoint", fake_endpoint)

    class _Proc:
        pid = 4321; returncode = 3
        def poll(self): return 3                      # our spawned daemon already exited (lost lock)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(supervisor, "_healthy", lambda transport: False)

    transport = supervisor.ensure_running()
    assert transport == Transport.tcp("127.0.0.1", 8765, "winner-token")


def test_endpoint_returns_unix_transport_from_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib, supervisor, paths
    importlib.reload(paths); importlib.reload(supervisor)
    paths.ensure_private_dir(paths.nelix_root())
    sock = paths.rpc_sock()
    supervisor._write_state(os.getpid(), Transport.unix(str(sock)))
    # health check must see a live daemon: stub _healthy True for this pid/transport.
    monkeypatch.setattr(supervisor, "_healthy", lambda t: True)
    ep = supervisor.endpoint()
    assert ep == Transport.unix(str(sock))


def test_write_state_is_0600_and_carries_transport(monkeypatch, tmp_path):
    import importlib, supervisor, paths, os, stat, json
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    paths.ensure_private_dir(paths.nelix_root())
    supervisor._write_state(4242, Transport.tcp("127.0.0.1", 55555, "tok"))
    st = json.loads(paths.state_file().read_text())
    assert st == {"pid": 4242, "transport": "tcp", "host": "127.0.0.1",
                  "port": 55555, "token": "tok"}
    assert stat.S_IMODE(os.stat(paths.state_file()).st_mode) == 0o600


def test_teardown_survives_ctrl_c_and_force_kills(monkeypatch, tmp_path):
    # Hermes' quit handler raises KeyboardInterrupt during teardown's graceful wait.
    # teardown must NOT propagate it, and must escalate to SIGKILL (force exit).
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    importlib.reload(supervisor)
    monkeypatch.setattr(supervisor, "_read_state",
                        lambda: {"pid": 4242, "port": 1, "token": "t"})
    alive = {"v": True}                                   # "process" dies only on SIGKILL
    monkeypatch.setattr(supervisor, "_pid_alive", lambda pid: alive["v"])
    signals = []

    def fake_kill(pid, sig):
        signals.append(sig)
        if sig == supervisor.signal.SIGKILL:
            alive["v"] = False
    monkeypatch.setattr(supervisor.os, "kill", fake_kill)
    monkeypatch.setattr(supervisor.os, "waitpid", lambda pid, flags=0: (0, 0))
    monkeypatch.setattr(supervisor.time, "sleep",
                        lambda _s: (_ for _ in ()).throw(KeyboardInterrupt()))

    supervisor.teardown("ctrl-c test")                    # must not raise
    assert supervisor.signal.SIGTERM in signals           # graceful attempt first
    assert supervisor.signal.SIGKILL in signals           # then force-killed
