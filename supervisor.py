"""Lifecycle of the ephemeral orchestration daemon.

Detached child, single state file under $NELIX_HOME (default ~/.nelix), SIGTERM->SIGKILL
teardown. (Modelled on plugins/google_meet/process_manager.py, back when this file lived
inside a Hermes plugin.)

THIS FILE IS TWO HALVES, and after the plugin extraction [nelix-4el.1] they have very
different standing in this repo. Read that before changing anything here:

  * DISCOVERY — endpoint, state_file, _read_state, _choose_transport, _healthy, _compatible,
    _live_lock_holder, _reconcile_lock_holder. LIVE and core: bin/nelix-doctor and
    bin/nelix-reap call supervisor.endpoint() to observe/reap a daemon they did not start.
    This is why supervisor.py stayed when the plugin left.

  * SPAWN — ensure_running, _daemon_argv. Nothing in bin/ calls these; only tests do. Their one
    production caller was the plugin's __init__.py:77 (supervisor.ensure_running()), which left
    for shady2k/hermes-nelix. So the spawn half is currently a Python API with no CLI and no
    production caller. It is NOT dead code: it is the raw material for `nelix daemon ensure`
    (nelix-3rm / Plan 3), the core entry point a harness is meant to call instead of
    reimplementing lifecycle — which is exactly the thing whose absence keeps the extracted
    plugin parked.

  * THE DEPS HACK — _ensure_deps, _venv_pip_install, _deps_present, _lazy_installs_allowed,
    _resolve_uv. GONE as of nelix-9a4.2; this note is here so it is not reinvented. It existed to
    make wasmtime importable in whatever interpreter was about to run daemon.app, back when the
    code arrived by living in a checkout that Hermes' venv could import but had never installed.
    That interpreter no longer exists. The core is a package: `import daemon.app` only resolves
    where nelix-core was installed, and installing it brings wasmtime with it (pyproject
    dependencies) — "has the code" and "has the deps" became the same act [nelix-9a4.1]. A
    generation gets both frozen into an immutable runtime by runtime.install(); a checkout gets
    both from `-e .`. There is no third case left for the hack to cover, and its `hermes_cli`
    import made a core module ask a harness for permission to install its own dependencies.

Naming debt, deliberately left alone in the extraction pass: PLUGIN_ROOT below is now the CORE
root. Its VALUE is right (it is the checkout, which is what the spawn needs when no runtime is
installed — verified: the daemon spawns, answers RPC and tears down from this repo with no plugin
present); only its NAME still says "plugin". Renaming it to CORE_ROOT is a pure rename and the
natural first step of the locator work [nelix-4el.1].
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

PLUGIN_ROOT = Path(__file__).parent          # the CORE root now — see the module docstring
_HEALTH_TIMEOUT = 10.0
_log = logging.getLogger("nelix.supervisor")

def _root() -> Path:
    return paths.nelix_root()


def _state_file() -> Path:
    return paths.state_file()


def state_file() -> Path:
    """Public path of the 0600 state file holding {pid, **transport}. The wake
    waiter reads the RPC token from here (see wake.arm_waiter)."""
    return _state_file()


def _daemon_argv():
    """The daemon's launch argv — and the point at which a generation is PINNED FOR ITS WHOLE LIFE.

    The interpreter chosen here becomes the daemon's `sys.executable`, and the daemon re-spawns its
    PTY broker with exactly that (`daemon/broker_client.py:31`). Aim it at an immutable runtime and
    every later respawn re-enters the SAME frozen code, even if a newer runtime has been installed
    and `current` has moved on since — which is what "old sessions keep running old code" actually
    reduces to. Aim it at a mutable checkout and it means nothing.

    No runtime installed = the checkout, where sys.executable is the venv that has the core on it
    (`-e .`). That is the dev loop, not a fallback: `runtime.active()` raises rather than land here
    if a runtime was named and is missing.
    """
    try:
        from .runtime import active_python
    except ImportError:           # loaded as a top-level module (tests), not as a package
        from runtime import active_python
    return [str(active_python() or sys.executable), "-m", "daemon.app"]


def _apply_code_source(env: dict) -> dict:
    """Set where the daemon child gets `daemon.*` from — and, under a runtime, where it MUST NOT.

    A runtime is only immutable if it is also the SOLE source of its code. This used to put
    PLUGIN_ROOT (the checkout) on the child's PYTHONPATH unconditionally, which is how a checkout
    makes `import daemon.app` work at all — but PYTHONPATH PRECEDES site-packages, so doing it to a
    runtime-launched daemon would hand generation N-1 the working tree's code and quietly undo
    everything the version-addressed directory bought: pinned in name only.

    A runtime therefore has PYTHONPATH SCRUBBED, not merely left un-injected — an inherited
    PYTHONPATH leaks a repo in just as well as one we add — plus user site-packages, which is
    another writable directory that outranks nothing but is still not part of the frozen closure.
    A checkout keeps the injection, because there the checkout IS the install.
    """
    if _active_build() is None:
        env["PYTHONPATH"] = str(PLUGIN_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")
        return env
    for leak in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(leak, None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _daemon_cwd() -> str:
    """cwd for the daemon child. The checkout runs IN the checkout (its cwd is a code source too,
    via sys.path[0]); a runtime must not, for the same reason it does not get PYTHONPATH."""
    return str(PLUGIN_ROOT) if _active_build() is None else str(paths.nelix_root())


def _active_build():
    try:
        from .runtime import active
    except ImportError:           # loaded as a top-level module (tests), not as a package
        from runtime import active
    return active()


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


def _choose_transport() -> Transport:
    import secrets
    if os.environ.get("NELIX_RPC_TRANSPORT") == "tcp":
        host = os.environ.get("NELIX_RPC_HOST", "127.0.0.1")
        return Transport.tcp(host, _free_port(), secrets.token_hex(16))
    return Transport.unix(str(paths.rpc_sock()))


# The owner the supervisor's PROBE speaks as. It owns nothing and never will: this probe wants
# one field, `rpc_protocol`, and that stamp happens to ride /status — a route that is now
# owner-filtered (daemon/owner.py) and so demands an owner from every caller, including one that
# does not care about sessions at all. An empty board is exactly the right answer here.
#
# The cleaner shape is a dedicated liveness/version route that exposes nothing session-derived
# and therefore needs no owner. Not done here: supervisor.py belongs to the router slice
# (nelix-3rm, see pyproject.toml), which is reworking this surface anyway, and a new public route
# added from this slice would be one it has to either keep or break.
_PROBE_OWNER = "nelix-supervisor-probe"


def _status_body(transport, timeout=2):
    """The daemon's /status JSON, or None if unreachable / non-200."""
    try:
        from .rpc_client import RpcClient
    except ImportError:           # loaded as a top-level module (tests), not as a package
        from rpc_client import RpcClient
    try:
        st, body = RpcClient(transport, _PROBE_OWNER)._call(
            "GET", f"/status?owner_id={_PROBE_OWNER}", timeout=timeout)
        return body if st == 200 else None
    except Exception:
        return None


def _compatible(status) -> bool:
    """True only for a /status from a daemon speaking OUR RPC protocol. A daemon left running on
    stale code reports a different — or missing — rpc_protocol, so it is incompatible and must be
    recycled rather than spoken past (the mismatch otherwise surfaces as RemoteDisconnected)."""
    return bool(status) and status.get("rpc_protocol") == RPC_PROTOCOL_VERSION


def _healthy(transport) -> bool:
    """A daemon answering /status with a COMPATIBLE protocol version. Protocol skew (old code) is
    treated as unhealthy so the reuse/adopt paths recycle it instead of talking past it."""
    return _compatible(_status_body(transport))


def _read_state():
    try:
        return json.loads(_state_file().read_text())
    except Exception:
        return None


def _write_state(pid: int, transport) -> None:
    root = _root()
    paths.ensure_private_dir(root)
    tmp = root / ".active.json.tmp"
    try:
        tmp.unlink()                          # clear a stale temp from a crashed prior write
    except FileNotFoundError:
        pass
    # 0600 AT creation (O_EXCL): the token never exists on disk world-readable, not even briefly.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        # Stamp the pid's start fingerprint so endpoint() can reject a recorded pid that has died
        # and been reused by an unrelated process (the symmetric guard to _live_lock_holder()).
        f.write(json.dumps({"pid": pid,
                            "start_fingerprint": reaper.ProcessInspector().start_fingerprint(pid),
                            **transport.to_state()}))
    tmp.replace(_state_file())


def endpoint():
    """Return the live daemon's Transport, or None if no healthy daemon is running."""
    st = _read_state()
    if not st:
        return None
    pid = st.get("pid")
    if not pid or not _pid_alive(pid):
        return None
    # Reject a recorded pid that died and was reused by an unrelated process: the fingerprint
    # (process start time) is immutable for a process's life, so a reused pid won't match.
    if reaper.ProcessInspector().start_fingerprint(pid) != st.get("start_fingerprint"):
        return None
    try:
        t = Transport.from_state(st)
    except ValueError:
        return None
    return t if _healthy(t) else None


def incarnation():
    """Identity of the live daemon's PROCESS INCARNATION — `{"pid", "start_fingerprint"}` — or
    None if no live, fingerprint-matched daemon is recorded. Surfaced from the SAME .active.json
    state endpoint() already trusts, with the same gate (pid alive AND its recorded start
    fingerprint still matches), so a recycled pid can never masquerade as the same incarnation.

    The router keys a generation EPOCH on this (nelix-3rm / spec §4: "a fresh generation epoch is
    minted on EVERY incarnation"): a restart changes the pid (or, on a reused pid, the start
    fingerprint), so the router observes a new incarnation and mints a new epoch — the daemon's own
    /health carries no incarnation id yet (that addition is a later slice), so supervisor identity
    is the key for 3c.1."""
    st = _read_state()
    if not st:
        return None
    pid = st.get("pid")
    if not pid or not _pid_alive(pid):
        return None
    fp = st.get("start_fingerprint")
    if reaper.ProcessInspector().start_fingerprint(pid) != fp:
        return None
    return {"pid": pid, "start_fingerprint": fp}


def active_generation():
    """The live daemon's `(transport, incarnation)` read from ONE validated .active.json
    snapshot, or None if no live, fingerprint-matched, HEALTHY daemon is recorded.

    This is `endpoint()` (transport + health) and `incarnation()` (identity) fused into a
    SINGLE state read, on purpose: the router keys a generation epoch on the incarnation and
    forwards over the transport, and reading the two separately lets a daemon RESTART land
    between them — binding a new incarnation's epoch to a dead incarnation's transport (a
    durable forward failure to a port/socket that moved). Reading both out of the same
    `_read_state()` closes that window: the pair is always from one incarnation.

    This is the FULL, slow read (it does a /health RPC). The router uses it OUTSIDE its epoch
    lock to ensure a generation is available; the cheap, lock-safe identity read it performs
    UNDER the lock is `current_generation()`."""
    st = _read_state()
    if not st:
        return None
    pid = st.get("pid")
    if not pid or not _pid_alive(pid):
        return None
    fp = st.get("start_fingerprint")
    if reaper.ProcessInspector().start_fingerprint(pid) != fp:
        return None
    try:
        t = Transport.from_state(st)
    except ValueError:
        return None
    if not _healthy(t):
        return None
    return t, {"pid": pid, "start_fingerprint": fp}


def current_generation():
    """The CHEAP read of the RECORDED generation's `(transport, incarnation)` — read straight from
    .active.json with NO /health RPC and NO start-fingerprint re-probe (`ps`), only a cheap
    pid-liveness check. Returns None if nothing live is recorded or the transport cannot be parsed.

    This reflects .active.json, which is NOT authoritative and NOT monotonic (a paused spawner can
    roll it back to a superseded incarnation — see held_generation()). It is therefore NOT the
    router's under-lock identity read; the router uses held_generation() (the validated live SINGLETON
    LOCK holder) for that. current_generation() survives as held_generation()'s building block for a
    TCP holder — whose lock meta carries no token — where .active.json is the only place the tokened
    transport lives, and held_generation() pairs it in ONLY when its incarnation matches the live lock
    holder (so a stale .active.json is never routed to). Splitting this cheap read out of
    active_generation() (which does the slow /health probe) keeps that reconciliation off the health
    RPC path."""
    st = _read_state()
    if not st:
        return None
    pid = st.get("pid")
    fp = st.get("start_fingerprint")
    if not pid or fp is None or not _pid_alive(pid):
        return None
    try:
        t = Transport.from_state(st)
    except ValueError:
        return None
    return t, {"pid": pid, "start_fingerprint": fp}


def held_generation():
    """The AUTHORITATIVE `(transport, incarnation)` of the daemon that currently HOLDS THE SINGLETON
    LOCK, or None if no live, fingerprint-matched daemon holds it (or no transport CONSISTENT with
    that holder can be derived). This is the router's UNDER-LOCK identity read (nelix-3rm 3c.1
    finding #1, rev 3).

    Why not current_generation()? .active.json is NOT authoritative and NOT monotonic. A spawner
    thread that validated a now-superseded incarnation A can PAUSE, let a newer B acquire the released
    singleton lock and publish itself, then RESUME and rewrite .active.json back to A — a ROLLBACK a
    bare .active.json read cannot detect (A's zombie still passes a bare pid-liveness check). The
    SINGLETON LOCK cannot roll back this way: the kernel guarantees exactly one live holder, and a
    released lock cannot be re-held by the dead pid. `_live_lock_holder()` (pid ALIVE — zombie-aware
    via ProcessInspector.is_alive — AND its recorded start fingerprint still matches) names the one
    current incarnation. Keying the epoch on THIS incarnation is what makes two concurrent callers
    mint exactly one epoch and keeps the registry's active pointer from rolling backward to A.

    TRANSPORT RECONCILIATION — the transport MUST belong to the SAME incarnation as the identity:
      * unix holder -> the lock meta records the socket path, so the holder's own transport is
        directly usable (a local unix socket is peercred/fs-gated, no token). Re-derived from the
        holder via `_holder_transport()`, never taken from a possibly-stale .active.json.
      * tcp holder -> the lock meta carries NO token, so the holder is not reachable from the lock
        alone. We accept .active.json's transport ONLY when its incarnation MATCHES the live lock
        holder's; a stale/mismatched .active.json yields None so the caller retries
        (GENERATION_UNAVAILABLE) rather than routing a start to a superseded incarnation's endpoint.

    Like current_generation(), this is the CHEAP, lock-safe read: a lock-file read + pid-liveness, no
    /health RPC and no spawn (the ensure OUTSIDE the registry lock already health-checked)."""
    meta = _live_lock_holder()
    if not meta:
        return None
    inc = {"pid": meta["pid"], "start_fingerprint": meta.get("start_fingerprint")}
    transport = _holder_transport(meta)
    if transport is not None:                     # unix: reachable directly from the lock meta
        return transport, inc
    # tcp holder: no token in the lock meta -> pair with .active.json ONLY when it is the SAME
    # incarnation (else its transport belongs to a superseded incarnation -> unavailable/retryable).
    recorded = current_generation()
    if recorded is not None and recorded[1] == inc:
        return recorded[0], inc
    return None


def _live_lock_holder():
    """The daemon.lock holder's metadata IF it names a live, fingerprint-matched process, else
    None. Fingerprint-matching (process start time) survives PID reuse, so a recycled pid can't
    masquerade as the daemon. This is the source of truth the start/teardown paths consult BEYOND
    .active.json, so an orphan daemon (crash, manual start, or one left on stale code after a
    plugin update) that holds the lock is never invisible to us."""
    meta = singleton.read_holder(paths.daemon_lock())
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


def _holder_transport(meta):
    """Best-effort Transport to reach the lock holder. A unix holder is reachable at the socket path
    it recorded in the lock meta (falling back to the default node for a holder predating the path
    stamp). A tcp holder is NOT reachable: the lock metadata carries no token, so we can't
    authenticate to it — such a holder can only be reaped, never adopted."""
    if meta.get("transport") == "unix":
        return Transport.unix(meta.get("path") or str(paths.rpc_sock()))
    return None


def _owns_lock(pid: int) -> bool:
    """True iff `pid` is the live, fingerprint-matched holder of daemon.lock — proof that THIS
    process owns the RPC endpoint, not an orphan answering the deterministic unix socket."""
    holder = _live_lock_holder()
    return bool(holder) and holder.get("pid") == pid


def _reap_daemon(pid: int, why: str) -> None:
    """SIGTERM->SIGKILL a daemon we are recycling. It is (almost always) not our child, so this
    leans on _graceful_wait/_force_kill's non-child handling. The SIGTERM trips the daemon's
    shutdown handler -> manager.stop_all(), so any live PTY sessions it owns are interrupted —
    hence the loud warning."""
    _log.warning("nelix daemon: reaping pid=%s (%s); any live sessions under it are interrupted",
                 pid, why)
    try:
        _graceful_wait(pid)
    except KeyboardInterrupt:
        pass
    _force_kill(pid)


def _await_endpoint(grace: float):
    """Poll endpoint() until it yields a healthy daemon or `grace` elapses. Lets a holder we cannot
    probe directly (a tcp daemon — the lock meta carries no token) prove itself by PUBLISHING a
    usable .active.json, which distinguishes a fresh concurrent winner (will publish) from a stale
    orphan (won't) — so we never reap a live winner just because its state write trails its lock."""
    deadline = time.time() + grace
    while time.time() < deadline:
        ep = endpoint()
        if ep is not None:
            return ep
        time.sleep(0.1)
    return None


def _reconcile_lock_holder():
    """Reconcile a singleton-lock holder that .active.json did NOT surface as a usable endpoint
    (an orphan from a crash, a manual start, or a daemon left on stale code after a plugin update).
    Returns a Transport to a holder we ADOPTED/reused, or None — None meaning we either REAPED an
    incompatible/stale holder or found none, and the caller should spawn a fresh daemon."""
    meta = _live_lock_holder()
    if not meta:
        return None
    transport = _holder_transport(meta)
    if transport is not None:                            # unix: we can probe /status directly
        if _healthy(transport):
            _write_state(meta["pid"], transport)         # adopt: alive AND speaks our protocol
            _log.info("nelix daemon: adopted lock holder pid=%s transport=unix", meta["pid"])
            return transport
        _reap_daemon(meta["pid"], "incompatible or unreachable unix lock holder")
        return None
    # tcp holder: the lock meta carries no token, so we cannot authenticate to /status. It is EITHER
    # a fresh concurrent winner about to publish .active.json OR a stale orphan. Let it prove itself
    # by publishing a usable endpoint within the health window; reap only if it never does.
    ep = _await_endpoint(_HEALTH_TIMEOUT)
    if ep is not None:
        return ep
    _reap_daemon(meta["pid"], "stale tcp lock holder (never published a usable endpoint)")
    return None


def _open_daemon_log(root) -> Path:
    """Create this spawn's 0600 log file under logs/, migrate any legacy root-level logs,
    point daemon-latest.log at it, prune old ones. <pid> is the supervisor's own pid (the
    daemon child pid only exists after Popen). Returns the per-spawn path for the child stdio."""
    logs = paths.logs_dir()
    paths.ensure_private_dir(logs)
    _migrate_legacy_logs(root, logs)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = paths.daemon_log(stamp, os.getpid())
    _create_private(log_path)
    _refresh_latest(log_path)
    retain = load_retention(str(paths.config_path())).daemon_log_retain
    _prune_daemon_logs(logs, retain, keep=log_path)
    return log_path


def _create_private(path) -> None:
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))   # 0600 file (replaces .touch())


def _migrate_legacy_logs(old_root, logs) -> None:
    """One-time: move pre-existing per-spawn logs from the old root into logs/, and drop a
    stale root-level daemon-latest.log symlink. Skips symlinks; best-effort."""
    if old_root == logs:
        return
    for p in old_root.glob(paths.DAEMON_LOG_GLOB):
        if p.is_symlink():
            continue
        try:
            p.replace(logs / p.name)
        except OSError:
            pass
    old_latest = old_root / "daemon-latest.log"
    if old_latest.is_symlink() or old_latest.exists():
        try:
            old_latest.unlink()
        except OSError:
            pass


def _refresh_latest(target) -> None:
    """Point <logs>/daemon-latest.log at `target` in the SAME dir (relative symlink), atomically."""
    d = target.parent
    link = d / "daemon-latest.log"
    tmp = d / "daemon-latest.log.tmp"
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


def ensure_running() -> Transport:
    existing = endpoint()
    if existing:
        return existing

    # .active.json yielded nothing usable, but a daemon may still hold the singleton lock+socket
    # (an orphan from a crash, a manual start, or one left on stale code after a plugin update).
    # Reconcile it BEFORE spawning: adopt it if it's live and compatible, else reap it so our fresh
    # daemon can take the lock — otherwise our spawn SystemExit(3)s on the held lock and the client
    # is left talking to the stale orphan (the RemoteDisconnected failure mode).
    adopted = _reconcile_lock_holder()
    if adopted:
        return adopted

    transport = _choose_transport()
    root = _root()
    paths.ensure_private_dir(root)
    log_path = _open_daemon_log(root)
    log = open(log_path, "ab")
    # NELIX_HOME is passed as the RESOLVED, CANONICAL root rather than left to the child's own
    # default, so the daemon cannot land on a different root than the supervisor that spawned it
    # (a relocated symlink between spawn and start would otherwise split them). Same reason
    # NELIX_CONFIG is passed resolved. HERMES_HOME is deliberately NOT set here any more: nothing
    # under daemon/ reads it (measured — zero hits), and materialising a harness's home for the
    # daemon and every executor it launches is precisely the coupling this slice removes. If a
    # Hermes harness has it set, it still reaches the child through os.environ below.
    env = _apply_code_source({**os.environ,
                              "NELIX_RPC_TRANSPORT": transport.kind,
                              "NELIX_CONFIG": str(paths.config_path()),
                              "NELIX_HOME": str(paths.nelix_root())})
    if transport.kind == "unix":
        env["NELIX_RPC_SOCK"] = transport.path
    else:
        env["NELIX_RPC_HOST"] = transport.host
        env["NELIX_RPC_PORT"] = str(transport.port)
        env["NELIX_RPC_TOKEN"] = transport.token
    try:
        proc = subprocess.Popen(
            _daemon_argv(), cwd=_daemon_cwd(), env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
    finally:
        log.close()  # parent's copy; child has its own inherited fd

    deadline = time.time() + _HEALTH_TIMEOUT
    while time.time() < deadline:
        # Require BOTH a compatible /status AND proof that OUR spawned pid holds the lock. A bare
        # health check on the deterministic unix socket could be answered by a *different* daemon;
        # recording that under proc.pid is exactly the ownership-attribution bug we are closing.
        if _healthy(transport) and _owns_lock(proc.pid):
            _write_state(proc.pid, transport)
            _log.info("nelix daemon started pid=%s transport=%s log=%s",
                      proc.pid, transport.kind, log_path)
            return transport
        if proc.poll() is not None:
            existing = endpoint()                    # we may have lost a singleton-lock race
            if existing:
                _log.info("nelix daemon: lost startup race, reusing pid-holder")
                return existing
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
        pids = []
        st = _read_state()
        if st and st.get("pid") and _pid_alive(st["pid"]):
            pids.append(st["pid"])
        # Also reap a live lock holder NOT named in .active.json — an orphan from a crash, a manual
        # start, or a daemon left on stale code. Otherwise it survives session-end and strands the
        # next nelix_start (the bug this hardening closes).
        holder = _live_lock_holder()
        if holder and holder["pid"] not in pids:
            pids.append(holder["pid"])
        interrupted = False
        for pid in pids:
            if not interrupted:
                try:
                    _graceful_wait(pid)
                except KeyboardInterrupt:
                    interrupted = True            # force exit now: skip graceful for remaining pids
            _force_kill(pid)                      # SIGKILL; no-op if it already exited
        try:
            _state_file().unlink()
        except Exception:
            pass
    except BaseException:                         # finalize hook never crashes the exit
        pass


def _graceful_wait(pid: int) -> None:
    """SIGTERM, then poll up to ~10s for the pid to exit. May raise KeyboardInterrupt
    if the wait is interrupted (the caller then force-kills)."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return                                    # raced us and already exited — nothing to wait on
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
