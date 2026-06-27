import faulthandler
import os
import signal
import sys

import paths
from daemon import reaper, singleton
from daemon.broker_client import BrokerClient, set_broker, get_broker
from daemon.config import (load_executors, load_concurrency_limit, load_retention,
                           load_log_level, load_kill_grace_seconds)
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
    if os.environ.get("NELIX_RPC_TRANSPORT") == "tcp":
        return Transport.tcp(os.environ.get("NELIX_RPC_HOST", "127.0.0.1"),
                             int(os.environ["NELIX_RPC_PORT"]),
                             os.environ["NELIX_RPC_TOKEN"])
    return Transport.unix(os.environ.get("NELIX_RPC_SOCK", str(paths.rpc_sock())))


def acquire_singleton(logger, transport=None):
    insp = reaper.ProcessInspector()
    pid = os.getpid()
    meta = {
        "pid": pid,
        "start_fingerprint": insp.start_fingerprint(pid),
        "transport": transport.kind if transport is not None else None,
        "port": transport.port if (transport is not None and transport.kind == "tcp") else None,
    }
    fd = singleton.acquire(paths.daemon_lock(), meta)
    if fd is None and logger is not None:
        holder = singleton.read_holder(paths.daemon_lock())
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
    transport = transport_from_env()
    _LOCK_FD = acquire_singleton(logger, transport=transport)
    if _LOCK_FD is None:
        raise SystemExit(3)               # another daemon owns this nelix_root
    set_broker(BrokerClient())            # spawn the broker BEFORE any threads exist
    grace = load_kill_grace_seconds(cfg_path)
    reaper_ctx = build_reaper_ctx(grace)
    events = EventQueue()
    retention = load_retention(cfg_path)
    manager = SessionManager(
        specs, events,
        launcher_factory=get_launcher, driver_factory=get_driver,
        concurrency_limit=limit, logger=logger,
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
