import os
import signal

from daemon.config import load_executors, load_concurrency_limit
from daemon.drivers import get_driver
from daemon.launchers import get_launcher
from daemon.events import EventQueue
from daemon.manager import SessionManager
from daemon.obs import Logger
from daemon.rpc_server import make_server


def install_shutdown_handler(manager):
    """SIGTERM -> graceful stop_all() then exit (mirrors the SIGINT path)."""
    def _handle(signum, frame):
        manager.stop_all()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handle)
    return _handle


def main():
    cfg_path = os.environ.get("NELIX_CONFIG", "nelix.toml")
    specs = load_executors(cfg_path)
    limit = load_concurrency_limit(cfg_path)
    for name, spec in specs.items():
        os.makedirs(spec.resolved_cwd(), exist_ok=True)
    logger = Logger()
    events = EventQueue()
    manager = SessionManager(
        specs, events,
        launcher_factory=get_launcher, driver_factory=get_driver,
        concurrency_limit=limit, logger=logger,
    )
    token = os.environ["NELIX_RPC_TOKEN"]
    port = int(os.environ.get("NELIX_RPC_PORT", "8765"))
    server = make_server(manager, token=token, port=port)
    logger.event("app", "info", msg=f"nelix daemon on 127.0.0.1:{port}",
                 executors=sorted(specs), limit=limit)
    install_shutdown_handler(manager)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        manager.stop_all()


if __name__ == "__main__":
    main()
