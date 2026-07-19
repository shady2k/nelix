"""Per-generation daemon lifecycle (ADDITIVE: production stays on the uid-wide singleton).

A ``GenerationSupervisor(generation_id, build_id)`` manages exactly one generation's daemon
subprocess, isolated in its OWN lock + state + socket. Two GenerationSupervisors with
different generation ids can each hold a live daemon at once with no lock conflict.

This is the FIRST half of the daemon-model transition (S1c-1). The existing uid-wide singleton
path in ``supervisor.py`` is UNCHANGED and still powers production; this module exists
alongside it, exercised only by new tests, until S1c-2 flips production over.

Greenfield: NO migration, legacy, backward-compat, dead code, or exception swallowing.
"""
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

try:
    from . import paths
    from .daemon.config import load_retention
    from .daemon.transport import Transport
    from .daemon.protocol import RPC_PROTOCOL_VERSION
    from .daemon import reaper, singleton
except ImportError:           # loaded as a top-level module (tests), not as a package
    import paths
    from daemon.config import load_retention
    from daemon.transport import Transport
    from daemon.protocol import RPC_PROTOCOL_VERSION
    from daemon import reaper, singleton

_HEALTH_TIMEOUT = 10.0
_log = logging.getLogger("nelix.gen_supervisor")

# The owner the generation supervisor's PROBE speaks as. Same rationale as
# supervisor._PROBE_OWNER — this probe wants one field and owns nothing.
_PROBE_OWNER = "nelix-gen-supervisor-probe"


class GenerationSupervisor:
    """Per-generation daemon lifecycle: spawn, discover, reconcile, teardown.

    Each instance is pinned to a specific ``generation_id`` and ``build_id``.
    The ``build_id`` is captured at construction and used for EVERY (re)spawn —
    never re-read from the global active runtime. This ensures a respawn runs the
    SAME code as the original spawn, even if ``runtimes/current`` has moved on.
    """

    def __init__(self, generation_id: str, build_id: "str | None" = None):
        from nelix_contracts.ids import validate_generation_id
        validate_generation_id(generation_id)

        self._generation_id = generation_id
        # Capture the build at construction — never re-read the global active
        # runtime. None means "use the checkout" (dev/test mode).
        self._build_id = build_id

        # Root directories derived from generation_id. The caller must ensure
        # these exist before spawn (see ensure_generation_dirs()).
        self._gen_dir = paths.generation_dir(generation_id)
        self._lock_path = paths.generation_lock(generation_id)
        self._state_path = paths.generation_state(generation_id)
        self._sock_path = str(paths.generation_sock(generation_id))

        # Paths to the global provisioned runtime directories.
        self._runtime_dir = paths.runtime_dir(build_id) if build_id else None
        self._runtime_python = paths.runtime_python(build_id) if build_id else None

    @property
    def generation_id(self) -> str:
        return self._generation_id

    @property
    def build_id(self) -> "str | None":
        return self._build_id

    # ---- directory setup ---------------------------------------------------

    def ensure_generation_dirs(self) -> None:
        """Create-or-verify the generation's state and runtime directories using
        the stronger owned + non-symlink check (ensure_owned_private_dir), not
        plain ensure_private_dir. Applies shallowest-first to each multi-level path.

        State path (nelix_root/generations/<gid>):
          - generations_root level first, then generation_dir level.
        Runtime path (/tmp/nelix-<uid>/gen-<hash>/<gid>):
          - per-uid base level, then hash level, then generation level.
        """
        # State dir: generations_root then generation_dir.
        paths.ensure_owned_private_dir(paths.generations_root())
        paths.ensure_owned_private_dir(self._gen_dir)

        # Runtime dir (short /tmp socket dir): parent levels first, then the leaf.
        sock_dir = paths.generation_runtime_dir(self._generation_id)
        for level in (sock_dir.parent.parent, sock_dir.parent, sock_dir):
            paths.ensure_owned_private_dir(level)

    # ---- path accessors (public, zero side effects) -------------------------

    def generation_dir(self) -> Path:
        return self._gen_dir

    def lock_path(self) -> Path:
        return self._lock_path

    def state_path(self) -> Path:
        return self._state_path

    def sock_path(self) -> str:
        return self._sock_path

    # ---- daemon launch ------------------------------------------------------

    def _daemon_argv(self):
        """The daemon's launch argv, PINNED to ``self._build_id`` at every call.

        When a build_id was captured, the daemon is spawned from THAT runtime's
        interpreter — never re-read from the global active runtime. This is the
        build-pinning requirement: a respawn runs the SAME code as the original
        spawn even if the global pointer has moved (Codex trap #3 in the spec).
        """
        if self._runtime_python is not None:
            return [str(self._runtime_python), "-m", "daemon.app"]
        # No build pinned: use the checkout (dev/test).
        return [sys.executable, "-m", "daemon.app"]

    def _daemon_cwd(self) -> str:
        """cwd for the daemon child. A pinned runtime runs from the nelix root
        (never from the checkout, which would import different code).
        """
        return (str(paths.nelix_root()) if self._build_id is not None
                else str(Path(__file__).parent))

    def _apply_code_source(self, env: dict) -> dict:
        """Set where the daemon child gets ``daemon.*`` from. A pinned runtime
        scrubs PYTHONPATH and PYTHONHOME (the runtime's site-packages is the
        sole source); a checkout injects the repo root on PYTHONPATH.
        """
        if self._build_id is None:
            env["PYTHONPATH"] = (str(Path(__file__).parent) + os.pathsep
                                 + os.environ.get("PYTHONPATH", ""))
            return env
        for leak in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(leak, None)
        env["PYTHONNOUSERSITE"] = "1"
        return env

    def _open_daemon_log(self) -> Path:
        """Create this spawn's 0600 log file under logs/, point the per-generation
        latest symlink at it, and prune old ones. The filename includes generation_id
        so two concurrent generations in the same clock tick never collide.
        """
        logs = paths.logs_dir()
        paths.ensure_private_dir(logs)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = paths.generation_log(self._generation_id, stamp, os.getpid())
        self._create_private(log_path)
        self._refresh_latest(log_path)
        retain = load_retention(str(paths.config_path())).daemon_log_retain
        self._prune_generation_logs(logs, retain, keep=log_path)
        return log_path

    @staticmethod
    def _create_private(path) -> None:
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))

    def _refresh_latest(self, target, generation_id=None) -> None:
        """Point <logs>/gen-<gid>-latest.log at `target` in the SAME dir (relative
        symlink), atomically.
        """
        gid = generation_id or self._generation_id
        d = target.parent
        link = d / f"gen-{gid}-latest.log"
        tmp = d / f"gen-{gid}-latest.log.tmp"
        try:
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            tmp.symlink_to(target.name)
            os.replace(tmp, link)
        except OSError:
            pass

    @staticmethod
    def _prune_generation_logs(root, retain, keep=None) -> None:
        files = [p for p in root.glob(paths.GENERATION_LOG_GLOB)
                 if not p.is_symlink() and (keep is None or p.name != keep.name)]
        files.sort(key=lambda p: p.stat().st_mtime)
        budget = retain - 1 if keep is not None else retain
        for p in files[:max(0, len(files) - max(0, budget))]:
            try:
                p.unlink()
            except OSError:
                pass

    def _choose_transport(self) -> Transport:
        """Transport for this generation. Always unix via the short runtime dir
        socket — per-generation daemons use a deterministic socket in the short
        /tmp namespace, not a random tcp port.
        """
        import secrets
        if os.environ.get("NELIX_RPC_TRANSPORT") == "tcp":
            host = os.environ.get("NELIX_RPC_HOST", "127.0.0.1")
            return Transport.tcp(host, self._free_port(), secrets.token_hex(16))
        return Transport.unix(self._sock_path)

    @staticmethod
    def _free_port() -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    # ---- state management ---------------------------------------------------

    def _read_state(self):
        """Read this generation's .active.json, or None on any error."""
        try:
            return json.loads(self._state_path.read_text())
        except Exception:
            return None

    def _write_state(self, pid: int, transport) -> None:
        """Write this generation's .active.json atomically via tmp+replace."""
        paths.ensure_private_dir(self._gen_dir)
        tmp = self._gen_dir / ".active.json.tmp"
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({
                "pid": pid,
                "start_fingerprint": reaper.ProcessInspector().start_fingerprint(pid),
                **transport.to_state(),
            }))
        tmp.replace(self._state_path)

    # ---- health / discovery -------------------------------------------------

    def _status_body(self, timeout=2):
        """The daemon's /status JSON, or None if unreachable / non-200."""
        try:
            from .rpc_client import RpcClient
        except ImportError:
            from rpc_client import RpcClient
        transport = Transport.unix(self._sock_path)
        try:
            st, body = RpcClient(transport, _PROBE_OWNER)._call(
                "GET", f"/status?owner_id={_PROBE_OWNER}", timeout=timeout)
            return body if st == 200 else None
        except Exception:
            return None

    def _compatible(self, status) -> bool:
        return bool(status) and status.get("rpc_protocol") == RPC_PROTOCOL_VERSION

    def _healthy(self) -> bool:
        """A daemon answering /status with a COMPATIBLE protocol version."""
        return self._compatible(self._status_body())

    def endpoint(self):
        """Return the live daemon's Transport, or None if no healthy daemon is running."""
        st = self._read_state()
        if not st:
            return None
        pid = st.get("pid")
        if not pid or not self._pid_alive(pid):
            return None
        if reaper.ProcessInspector().start_fingerprint(pid) != st.get("start_fingerprint"):
            return None
        try:
            t = Transport.from_state(st)
        except ValueError:
            return None
        return t if self._check_health(t) else None

    def _check_health(self, transport) -> bool:
        """Check a transport for compatibility. Same as _healthy() but takes
        an explicit transport (used after spawn to check OUR daemon).
        """
        try:
            from .rpc_client import RpcClient
        except ImportError:
            from rpc_client import RpcClient
        try:
            st, body = RpcClient(transport, _PROBE_OWNER)._call(
                "GET", f"/status?owner_id={_PROBE_OWNER}", timeout=2)
            return self._compatible(body) if st == 200 else False
        except Exception:
            return False

    def _live_lock_holder(self):
        """The per-generation lock holder's metadata IF it names a live,
        fingerprint-matched process, else None.
        """
        meta = singleton.read_holder(self._lock_path)
        if not meta:
            return None
        pid = meta.get("pid")
        if not pid:
            return None
        insp = reaper.ProcessInspector()
        if not insp.is_alive(pid):
            return None
        if insp.start_fingerprint(pid) != meta.get("start_fingerprint"):
            return None
        return meta

    def _owns_lock(self, pid: int) -> bool:
        holder = self._live_lock_holder()
        return bool(holder) and holder.get("pid") == pid

    def _health_identity(self, transport) -> "dict | None":
        """Read the daemon's /health and return ``{generation_id, generation_epoch}``
        if reachable, or None. Used by strict adoption to reject a mismatched holder.
        """
        try:
            from .rpc_client import RpcClient
        except ImportError:
            from rpc_client import RpcClient
        try:
            health = RpcClient(transport, _PROBE_OWNER).health(timeout=2)
            return {
                "generation_id": health.get("generation_id"),
                "generation_epoch": health.get("generation_epoch"),
            }
        except Exception:
            return None

    # ---- ensure running -----------------------------------------------------

    def ensure_running(self, generation_epoch: "str | None" = None) -> Transport:
        """Spawn or discover this generation's daemon. Returns a Transport
        to a healthy per-generation daemon.

        ``generation_epoch``, if provided, is passed to the child via
        ``NELIX_GENERATION_EPOCH`` so the daemon publishes it on /health.
        """
        existing = self.endpoint()
        if existing:
            return existing

        # Reconcile a lock holder that .active.json did NOT surface.
        adopted = self._reconcile_lock_holder()
        if adopted:
            return adopted

        transport = self._choose_transport()
        log_path = self._open_daemon_log()
        log = open(log_path, "ab")

        env = self._apply_code_source({**os.environ,
                                       "NELIX_RPC_TRANSPORT": transport.kind,
                                       "NELIX_CONFIG": str(paths.config_path()),
                                       "NELIX_HOME": str(paths.nelix_root()),
                                       "NELIX_GENERATION_ID": self._generation_id,
                                       })
        if generation_epoch is not None:
            env["NELIX_GENERATION_EPOCH"] = generation_epoch
        if transport.kind == "unix":
            env["NELIX_RPC_SOCK"] = transport.path
        else:
            env["NELIX_RPC_HOST"] = transport.host
            env["NELIX_RPC_PORT"] = str(transport.port)
            env["NELIX_RPC_TOKEN"] = transport.token

        try:
            proc = subprocess.Popen(
                self._daemon_argv(), cwd=self._daemon_cwd(), env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True)
        finally:
            log.close()

        deadline = time.time() + _HEALTH_TIMEOUT
        while time.time() < deadline:
            # Require BOTH a compatible /status AND proof that OUR spawned pid
            # holds the per-generation lock.
            if self._check_health(transport) and self._owns_lock(proc.pid):
                self._write_state(proc.pid, transport)
                _log.info("generation daemon started gen_id=%s pid=%s transport=%s log=%s",
                          self._generation_id, proc.pid, transport.kind, log_path)
                return transport
            if proc.poll() is not None:
                existing = self.endpoint()
                if existing:
                    _log.info("generation daemon: lost startup race pid=%s", proc.pid)
                    return existing
                raise RuntimeError(
                    f"generation daemon gen_id={self._generation_id} exited early "
                    f"(code {proc.returncode}); see {log_path}")
            time.sleep(0.1)
        proc.terminate()
        raise RuntimeError(
            f"generation daemon gen_id={self._generation_id} did not become healthy; "
            f"see {log_path}")

    # ---- reconciliation / adoption ------------------------------------------

    def _reconcile_lock_holder(self):
        """Reconcile a lock holder that .active.json did NOT surface as a usable
        endpoint. Returns a Transport to a holder we ADOPTED, or None.

        Strict identity adoption: a lock holder whose /health identity does NOT
        match ``{generation_id, generation_epoch}`` (the holder's own env) is
        REAPED rather than adopted — it belongs to a different generation and
        must not be confused with ours.
        """
        meta = self._live_lock_holder()
        if not meta:
            return None

        transport = self._holder_transport(meta)
        if transport is None:
            # tcp holder: no token in lock meta, cannot probe. Wait for it to
            # publish a usable endpoint.
            ep = self._await_endpoint(_HEALTH_TIMEOUT)
            if ep is not None:
                return ep
            self._reap_daemon(meta["pid"], "stale tcp lock holder (never published)")
            return None

        # unix holder: probe /health for identity match.
        if self._check_health(transport):
            identity = self._health_identity(transport)
            if identity is not None:
                actual_gid = identity.get("generation_id")
                if actual_gid == self._generation_id:
                    # Identity matches — adopt.
                    self._write_state(meta["pid"], transport)
                    _log.info("generation daemon: adopted lock holder gen_id=%s pid=%s",
                              self._generation_id, meta["pid"])
                    return transport
                _log.warning("generation daemon: lock holder gen_id=%s pid=%s has "
                             "mismatched identity (expected %s, got %s); reaping",
                             self._generation_id, meta["pid"],
                             self._generation_id, actual_gid)
            else:
                # /health unreachable — treat as incompatible.
                _log.warning("generation daemon: lock holder gen_id=%s pid=%s "
                             "unreachable; reaping",
                             self._generation_id, meta["pid"])
            self._reap_daemon(meta["pid"],
                              f"incompatible lock holder for gen_id={self._generation_id}")
        else:
            self._reap_daemon(meta["pid"],
                              f"unhealthy lock holder for gen_id={self._generation_id}")
        return None

    def _holder_transport(self, meta):
        """Best-effort Transport to reach the lock holder."""
        if meta.get("transport") == "unix":
            return Transport.unix(meta.get("path") or self._sock_path)
        return None

    def _await_endpoint(self, grace: float):
        deadline = time.time() + grace
        while time.time() < deadline:
            ep = self.endpoint()
            if ep is not None:
                return ep
            time.sleep(0.1)
        return None

    @staticmethod
    def _reap_daemon(pid: int, why: str) -> None:
        """SIGTERM->SIGKILL a daemon we are recycling."""
        _log.warning("generation daemon: reaping pid=%s (%s)", pid, why)
        try:
            _graceful_wait(pid)
        except KeyboardInterrupt:
            pass
        _force_kill(pid)

    # ---- teardown -----------------------------------------------------------

    def teardown(self, reason: str = "") -> None:
        """Kill this generation's daemon and clear its state."""
        _log.info("generation daemon teardown gen_id=%s: %s", self._generation_id,
                  reason or "(no reason)")
        try:
            pids = []
            st = self._read_state()
            if st and st.get("pid") and self._pid_alive(st["pid"]):
                pids.append(st["pid"])
            holder = self._live_lock_holder()
            if holder and holder["pid"] not in pids:
                pids.append(holder["pid"])
            interrupted = False
            for pid in pids:
                if not interrupted:
                    try:
                        _graceful_wait(pid)
                    except KeyboardInterrupt:
                        interrupted = True
                _force_kill(pid)
            try:
                self._state_path.unlink()
            except Exception:
                pass
        except BaseException:
            pass


# ---- shared helpers (same logic as supervisor.py) --------------------------

def _graceful_wait(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(20):
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return
        except ChildProcessError:
            pass
        if not _pid_alive(pid):
            return
        time.sleep(0.5)


def _force_kill(pid: int) -> None:
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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
