"""Lifecycle of the ephemeral orchestration daemon (one per Hermes gateway).

Modelled on plugins/google_meet/process_manager.py: detached child, single
state file under $HERMES_HOME/workspace/nelix/, SIGTERM->SIGKILL teardown. The daemon is
NOT meant to survive a Hermes restart (BR-11 dropped) — on_session_end tears it
down; a fresh one is spawned on the next nelix_start.
"""
import importlib
import importlib.util
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    from . import paths
    from .daemon.config import load_retention
except ImportError:           # loaded as a top-level module (tests), not as a package
    import paths
    from daemon.config import load_retention

PLUGIN_ROOT = Path(__file__).parent
_HEALTH_TIMEOUT = 10.0
_log = logging.getLogger("nelix.supervisor")

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


def _venv_pip_install(specs):
    """Install *specs* into the active venv (sys.executable) via a uv -> pip ->
    ensurepip ladder, mirroring Hermes' lazy installer (tools/lazy_deps.py) so it
    also works in a uv-managed venv that ships no pip. Returns (ok, output)."""
    venv_root = Path(sys.executable).parent.parent
    env = {**os.environ, "VIRTUAL_ENV": str(venv_root)}
    # Tier 1: uv (fast; does not need pip inside the venv).
    uv = shutil.which("uv")
    if uv:
        try:
            r = subprocess.run([uv, "pip", "install", *specs], env=env,
                               capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=300)
            if r.returncode == 0:
                return True, (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.SubprocessError):
            pass  # fall through to pip
    # Tier 2: python -m pip (bootstrap via ensurepip if pip is absent).
    pip = [sys.executable, "-m", "pip"]
    need_bootstrap = False
    try:
        probe = subprocess.run(pip + ["--version"], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=30)
        need_bootstrap = probe.returncode != 0
    except (OSError, subprocess.SubprocessError):
        need_bootstrap = True
    if need_bootstrap:
        try:
            subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"pip unavailable and ensurepip bootstrap failed: {e}"
    try:
        r = subprocess.run(pip + ["install", *specs], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=300)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"pip install failed: {e}"


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
    ok, output = _venv_pip_install(_DAEMON_DEPS)
    importlib.invalidate_caches()
    if not ok or not _deps_present():
        raise RuntimeError(
            "nelix daemon dependency install failed; install manually: "
            + manual + "\n" + (output or "")[-1000:].strip())


def _root() -> Path:
    return paths.nelix_root()


def _state_file() -> Path:
    return paths.state_file()


def state_file() -> Path:
    """Public path of the 0600 state file holding {pid,port,token}. The wake
    waiter reads the RPC token from here (see wake.arm_waiter)."""
    return _state_file()


def _daemon_argv():
    return [sys.executable, "-m", "daemon.app"]


def _free_port() -> int:
    # Bind-to-0, read port, close, then rebind — accepted MVP TOCTOU tradeoff:
    # loopback only, window is tiny; health-timeout fails loudly if rebind loses.
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


def _open_daemon_log(root) -> Path:
    """Create this spawn's log file, point daemon-latest.log at it, prune old ones.
    <pid> is the supervisor's own pid (known pre-spawn; the daemon child pid only
    exists after Popen). Returns the per-spawn path to open for the child's stdio."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = paths.daemon_log(stamp, os.getpid())
    log_path.touch()
    _refresh_latest(root, log_path)
    retain = load_retention(str(paths.config_path())).daemon_log_retain
    _prune_daemon_logs(root, retain, keep=log_path)
    return log_path


def _refresh_latest(root, target) -> None:
    link = paths.daemon_latest()
    tmp = root / "daemon-latest.log.tmp"
    try:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        tmp.symlink_to(target.name)        # relative (same dir) target
        os.replace(tmp, link)              # atomic overwrite of the existing symlink
    except OSError:
        pass


def _prune_daemon_logs(root, retain, keep=None) -> None:
    # Never a deletion candidate: the daemon-latest.log symlink, and the just-created
    # file (`keep`) — it must survive regardless of any odd/future mtime on older files.
    files = [p for p in root.glob(paths.DAEMON_LOG_GLOB)
             if not p.is_symlink() and (keep is None or p.name != keep.name)]
    files.sort(key=lambda p: p.stat().st_mtime)            # oldest first
    budget = retain - 1 if keep is not None else retain    # `keep` occupies one slot of `retain`
    for p in files[:max(0, len(files) - max(0, budget))]:
        try:
            p.unlink()
        except OSError:
            pass


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
    log_path = _open_daemon_log(root)
    log = open(log_path, "ab")
    env = {**os.environ,
           "NELIX_RPC_TOKEN": token,
           "NELIX_RPC_PORT": str(port),
           "NELIX_CONFIG": str(paths.config_path()),
           "HERMES_HOME": str(paths.hermes_home()),
           "PYTHONPATH": str(PLUGIN_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    try:
        proc = subprocess.Popen(
            _daemon_argv(), cwd=str(PLUGIN_ROOT), env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
    finally:
        log.close()  # parent's copy; child has its own inherited fd

    deadline = time.time() + _HEALTH_TIMEOUT
    while time.time() < deadline:
        if _healthy(port, token):
            _write_state(proc.pid, port, token)
            _log.info("nelix daemon started pid=%s port=%s log=%s", proc.pid, port, log_path)
            return f"http://127.0.0.1:{port}", token
        if proc.poll() is not None:
            raise RuntimeError(
                f"nelix daemon exited early (code {proc.returncode}); see {log_path}")
        time.sleep(0.1)
    proc.terminate()
    raise RuntimeError(f"nelix daemon did not become healthy; see {log_path}")


def teardown(reason: str = "") -> None:
    # Runs as Hermes' session-finalize hook on exit. It MUST NOT propagate — Hermes'
    # quit signal handler raises KeyboardInterrupt (a BaseException), which during our
    # graceful wait would otherwise escape as a bare traceback. A Ctrl+C here means
    # "force exit now": cut the graceful wait short and go straight to SIGKILL.
    _log.info("nelix daemon teardown: %s", reason or "(no reason)")
    try:
        st = _read_state()
        if st and _pid_alive(st["pid"]):
            pid = st["pid"]
            try:
                _graceful_wait(pid)
            except KeyboardInterrupt:
                pass                              # force exit -> fall through to SIGKILL
            _force_kill(pid)                      # no-op if it already exited
        try:
            _state_file().unlink()
        except Exception:
            pass
    except BaseException:                         # finalize hook never crashes the exit
        pass


def _graceful_wait(pid: int) -> None:
    """SIGTERM, then poll up to ~10s for the pid to exit. May raise KeyboardInterrupt
    if the wait is interrupted (the caller then force-kills)."""
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        try:                                      # reap if it's OUR child
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return
        except ChildProcessError:
            pass                                  # cross-process: not our child
        if not _pid_alive(pid):                   # handles the non-child case
            return
        time.sleep(0.5)


def _force_kill(pid: int) -> None:
    """SIGKILL if still alive; reap if it's our child. Best-effort, never raises."""
    if not _pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    except (KeyboardInterrupt, Exception):
        pass
