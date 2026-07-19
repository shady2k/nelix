import faulthandler
import os
import signal
import sys

import paths
from daemon import reaper, singleton
from daemon.broker_client import BrokerClient, set_broker, get_broker
from daemon.config import (load_executors, load_concurrency_limit, load_idle_retained_limit,
                           load_retention, load_log_level, load_kill_grace_seconds,
                           load_event_ring)
from daemon.drivers import get_driver
from daemon.launchers import get_launcher
from daemon.events import EventQueue
from daemon.manager import SessionManager
from daemon.obs import Logger
from daemon.rpc_server import make_server
from daemon.transport import Transport

_LOCK_FD = None   # held for the daemon's lifetime: closing it releases the singleton flock


def build_reaper_ctx(grace):
    insp = reaper.ProcessInspector()
    pid = os.getpid()
    return reaper.ReaperContext(daemon_pid=pid, daemon_fingerprint=insp.start_fingerprint(pid),
                                grace=grace, inspector=insp, killer=reaper.ProcessKiller())


def transport_from_env():
    """Return a Transport from env vars. NELIX_RPC_SOCK is REQUIRED for unix;
    NELIX_RPC_HOST/NELIX_RPC_PORT/NELIX_RPC_TOKEN for TCP."""
    if os.environ.get("NELIX_RPC_TRANSPORT") == "tcp":
        return Transport.tcp(os.environ.get("NELIX_RPC_HOST", "127.0.0.1"),
                             int(os.environ["NELIX_RPC_PORT"]),
                             os.environ["NELIX_RPC_TOKEN"])
    # S1c-2: NELIX_RPC_SOCK is REQUIRED for per-generation daemons.
    sock = os.environ.get("NELIX_RPC_SOCK")
    if not sock:
        raise RuntimeError(
            "NELIX_RPC_SOCK is required for per-generation daemons; "
            "the uid-wide rpc_sock fallback has been removed")
    return Transport.unix(sock)


def acquire_singleton(logger, transport=None):
    insp = reaper.ProcessInspector()
    pid = os.getpid()
    meta = {
        "pid": pid,
        "start_fingerprint": insp.start_fingerprint(pid),
        "transport": transport.kind if transport is not None else None,
        "port": transport.port if (transport is not None and transport.kind == "tcp") else None,
        # Unix socket path is symmetric to the tcp port: a daemon started with a custom
        # NELIX_RPC_SOCK can hold the lock while serving a non-default node, so the supervisor
        # adopts/probes the path the holder ACTUALLY bound rather than assuming the default.
        "path": transport.path if (transport is not None and transport.kind == "unix") else None,
    }
    # S1c-2: NELIX_GENERATION_ID is REQUIRED for per-generation daemons.
    gid = os.environ.get("NELIX_GENERATION_ID")
    if not gid:
        raise RuntimeError(
            "NELIX_GENERATION_ID is required for per-generation daemons; "
            "the uid-wide daemon_lock fallback has been removed")
    from nelix_contracts.ids import validate_generation_id
    validate_generation_id(gid)
    # S1c-2 / H10: NELIX_GENERATION_EPOCH is also REQUIRED and must be a valid
    # generation-id-shaped string (same shape as generation_id).
    epoch = os.environ.get("NELIX_GENERATION_EPOCH")
    if not epoch:
        raise RuntimeError(
            "NELIX_GENERATION_EPOCH is required for per-generation daemons")
    from nelix_contracts.ids import validate_generation_id as _validate_gid
    try:
        _validate_gid(epoch)
    except Exception:
        raise ValueError(
            f"NELIX_GENERATION_EPOCH must be a valid generation-id-shaped string, "
            f"got {epoch!r}") from None
    lock_path = paths.generation_lock(gid)
    fd = singleton.acquire(lock_path, meta)
    if fd is None and logger is not None:
        holder = singleton.read_holder(lock_path)
        logger.warning("app", "daemon_lock_conflict", holder=holder)
    return fd


def install_stack_dump_handler():
    """`kill -USR1 <pid>` dumps every thread's stack to the daemon log (stderr ->
    daemon-*.log). For diagnosing a wedged monitor thread without py-spy/sudo.
    Also dump on fatal signals (segfault etc.)."""
    faulthandler.enable(file=sys.stderr, all_threads=True)
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, file=sys.stderr,
                              all_threads=True, chain=False)


def warn_invalid_log_level(logger, level_cfg):
    """Emit one warning iff the WINNING log-level source (env or file) was invalid."""
    if level_cfg.invalid_value is not None:
        logger.warning("app", "invalid_log_level", value=level_cfg.invalid_value,
                       source=level_cfg.invalid_source, using=level_cfg.level)


def install_shutdown_handler(manager, logger=None):
    """SIGTERM -> graceful stop_all() then exit (mirrors the SIGINT path)."""
    def _handle(signum, frame):
        if logger is not None:
            logger.info("app", "shutdown_requested", signal=signum)
        manager.stop_all()
        try:
            get_broker().close()
        except Exception:
            pass
        if logger is not None:
            logger.info("app", "shutdown_complete", signal=signum)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handle)
    return _handle


def load_specs(cfg_path, logger):
    """Resilient executor load for the daemon: serve the valid executors, log every skip and
    any whole-file parse error as a warning. Never raises — a bad config must not crash the daemon."""
    loaded = load_executors(cfg_path)
    if loaded.parse_error and logger is not None:
        logger.warning("app", "config_parse_error", error=loaded.parse_error)
    for e in loaded.executor_errors:
        if logger is not None:
            logger.warning("app", "executor_skipped", executor=e["name"], problem=e["problem"])
    return loaded.specs


def main():
    global _LOCK_FD
    cfg_path = os.environ.get("NELIX_CONFIG", "nelix.toml")
    level_cfg = load_log_level(cfg_path)
    logger = Logger(level=level_cfg.level)
    specs = load_specs(cfg_path, logger)
    limit = load_concurrency_limit(cfg_path)
    idle_limit = load_idle_retained_limit(cfg_path, default=limit)
    transport = transport_from_env()
    _LOCK_FD = acquire_singleton(logger, transport=transport)
    if _LOCK_FD is None:
        raise SystemExit(3)               # another daemon owns this NELIX_HOME
    set_broker(BrokerClient())            # spawn the broker BEFORE any threads exist
    grace = load_kill_grace_seconds(cfg_path)
    reaper_ctx = build_reaper_ctx(grace)
    ring = load_event_ring(cfg_path)
    events = EventQueue(max_history=ring.max_history, owner_floor=ring.owner_floor)
    retention = load_retention(cfg_path)
    # nelix-9a4.4: import locally so the Store import does not force a pyproject dep —
    # nelix_store ships alongside the core wheel via the runtime installer.
    from nelix_store.store import Store
    manager = SessionManager(
        specs, events,
        Store(paths.nelix_root()),
        launcher_factory=get_launcher, driver_factory=get_driver,
        concurrency_limit=limit, idle_retained_limit=idle_limit, logger=logger,
        session_retain=retention.session_retain,
        session_max_age_days=retention.session_max_age_days,
        reaper_ctx=reaper_ctx,
    )
    reaper.reconcile_orphans(paths.sessions_root(), reaper_ctx.daemon_pid,
                             reaper_ctx.daemon_fingerprint, grace,
                             reaper_ctx.inspector, reaper_ctx.killer, logger=logger)
    server = make_server(manager, transport, logger=logger)
    logger.info("app", "daemon_started", executors=sorted(specs), limit=limit,
                log_level=level_cfg.level, transport=transport.kind)
    warn_invalid_log_level(logger, level_cfg)
    install_stack_dump_handler()
    install_shutdown_handler(manager, logger)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        manager.stop_all()
    finally:
        try:
            get_broker().close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
