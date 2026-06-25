import faulthandler
import os
import signal
import sys

from daemon.config import (load_executors, load_concurrency_limit, load_retention,
                           load_log_level)
from daemon.drivers import get_driver
from daemon.launchers import get_launcher
from daemon.events import EventQueue
from daemon.manager import SessionManager
from daemon.obs import Logger
from daemon.rpc_server import make_server


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
        if logger is not None:
            logger.info("app", "shutdown_complete", signal=signum)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handle)
    return _handle


def main():
    cfg_path = os.environ.get("NELIX_CONFIG", "nelix.toml")
    specs = load_executors(cfg_path)
    limit = load_concurrency_limit(cfg_path)
    level_cfg = load_log_level(cfg_path)
    logger = Logger(level=level_cfg.level)
    events = EventQueue()
    retention = load_retention(cfg_path)
    manager = SessionManager(
        specs, events,
        launcher_factory=get_launcher, driver_factory=get_driver,
        concurrency_limit=limit, logger=logger,
        session_retain=retention.session_retain,
        session_max_age_days=retention.session_max_age_days,
    )
    token = os.environ["NELIX_RPC_TOKEN"]
    port = int(os.environ.get("NELIX_RPC_PORT", "8765"))
    server = make_server(manager, token=token, port=port, logger=logger)
    logger.info("app", "daemon_started", executors=sorted(specs), limit=limit,
                log_level=level_cfg.level, port=port)
    warn_invalid_log_level(logger, level_cfg)
    install_stack_dump_handler()
    install_shutdown_handler(manager, logger)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        manager.stop_all()


if __name__ == "__main__":
    main()
