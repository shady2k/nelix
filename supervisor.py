"""Lifecycle of the ephemeral orchestration daemon (one per Hermes gateway).

Modelled on plugins/google_meet/process_manager.py: detached child, single
state file under $HERMES_HOME/nelix/, SIGTERM->SIGKILL teardown. The daemon is
NOT meant to survive a Hermes restart (BR-11 dropped) — on_session_end tears it
down; a fresh one is spawned on the next nelix_start.
"""
import importlib
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import registry

PLUGIN_ROOT = Path(__file__).parent
_HEALTH_TIMEOUT = 10.0

# Daemon deps live in the Hermes runtime venv (our sys.executable), which does
# not ship them. No plugin.yaml field installs deps (pip_dependencies is a
# no-op) — self-install venv-scoped, exactly like plugins/google_meet/cli.py.
_DAEMON_DEPS = ("pyte==0.8.2", "ptyprocess==0.7.0")
_DAEMON_MODULES = ("pyte", "ptyprocess")


def _deps_present() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in _DAEMON_MODULES)


def _lazy_installs_allowed() -> bool:
    if os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1":
        return False
    try:
        from hermes_cli.config import load_config
        return bool((load_config().get("security") or {}).get("allow_lazy_installs", True))
    except Exception:
        return True


def _ensure_deps() -> None:
    """Make pyte/ptyprocess importable in the Hermes runtime venv (sys.executable).

    Honors the same security gate as Hermes' lazy installer. Raises RuntimeError
    with a manual-pip hint if installs are disabled or fail."""
    if _deps_present():
        return
    manual = f"{sys.executable} -m pip install " + " ".join(_DAEMON_DEPS)
    if not _lazy_installs_allowed():
        raise RuntimeError(
            "nelix daemon needs " + " ".join(_DAEMON_DEPS)
            + " but lazy installs are disabled (security.allow_lazy_installs=false). "
            + f"Install manually: {manual}")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", *_DAEMON_DEPS],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    importlib.invalidate_caches()
    if proc.returncode != 0 or not _deps_present():
        raise RuntimeError(
            "nelix daemon dependency install failed; install manually: "
            + manual + "\n" + (proc.stderr or proc.stdout or "")[-1000:].strip())


def _root() -> Path:
    return registry.hermes_home() / "nelix"


def _state_file() -> Path:
    return _root() / ".active.json"


def _daemon_argv():
    return [sys.executable, "-m", "daemon.app"]


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _healthy(port: int, token: str) -> bool:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/status",
                                 headers={"X-Nelix-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _read_state():
    try:
        return json.loads(_state_file().read_text())
    except Exception:
        return None


def _write_state(pid: int, port: int, token: str) -> None:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / ".active.json.tmp"
    tmp.write_text(json.dumps({"pid": pid, "port": port, "token": token}))
    os.chmod(tmp, 0o600)
    tmp.replace(_state_file())


def base_token():
    st = _read_state()
    if st and _pid_alive(st["pid"]) and _healthy(st["port"], st["token"]):
        return f"http://127.0.0.1:{st['port']}", st["token"]
    return None


def ensure_running():
    existing = base_token()
    if existing:
        return existing

    _ensure_deps()  # daemon imports pyte/ptyprocess; install them venv-scoped if absent

    import secrets
    token = secrets.token_hex(16)
    port = _free_port()
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    log = open(root / "daemon.log", "ab")
    env = {**os.environ,
           "NELIX_RPC_TOKEN": token,
           "NELIX_RPC_PORT": str(port),
           "NELIX_CONFIG": str(registry.config_path()),
           "HERMES_HOME": str(registry.hermes_home()),
           "PYTHONPATH": str(PLUGIN_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.Popen(
        _daemon_argv(), cwd=str(PLUGIN_ROOT), env=env,
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True, close_fds=True)

    deadline = time.time() + _HEALTH_TIMEOUT
    while time.time() < deadline:
        if _healthy(port, token):
            _write_state(proc.pid, port, token)
            return f"http://127.0.0.1:{port}", token
        if proc.poll() is not None:
            raise RuntimeError(
                f"nelix daemon exited early (code {proc.returncode}); see {root/'daemon.log'}")
        time.sleep(0.1)
    proc.terminate()
    raise RuntimeError(f"nelix daemon did not become healthy; see {root/'daemon.log'}")


def teardown(reason: str = "") -> None:
    st = _read_state()
    if st and _pid_alive(st["pid"]):
        pid = st["pid"]
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if not _pid_alive(pid):
                    break
                time.sleep(0.5)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
                time.sleep(0.1)
            # Reap the zombie so os.kill(pid, 0) raises OSError for callers.
            try:
                os.waitpid(pid, 0)
            except Exception:
                pass
        except Exception:
            pass
    try:
        _state_file().unlink()
    except Exception:
        pass
