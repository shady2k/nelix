"""Router bootstrap (spec §1): establish the SECURE runtime dir + exclusive lock, build the ONE
shared StartLedger + generation registry + start path, and serve the control-plane HTTP API.

The router holds no master fds and streams nothing (spec §1: control-plane, not data-plane), so its
restart is a moment of connection refusal + a client retry — never a killed PTY. A SECOND router on
the same NELIX_HOME loses the flock (RouterLockHeld) and exits cleanly (code 3), rather than binding
a competing public socket.

Runs from the checkout for now (like supervisor.py / rpc_client.py, it is not in the shipped wheel);
wiring it into a `nelix router` entry point / deployment is a later slice.
"""
import logging
import signal
import sys
import uuid

import paths
from nelix_store.ledger import StartLedger
from router.registry import GenerationRegistry
from router.runtime_dir import RouterLockHeld, establish
from router.server import make_router_server
from router.start import StartPath

_log = logging.getLogger("nelix.router")


def _new_router_epoch() -> str:
    """A fresh per-process router epoch (spec §4: a router restart changes router_epoch, expiring
    old cursors). r-<32hex>, minted once at startup."""
    return "r-" + uuid.uuid4().hex


def _install_shutdown_handlers() -> None:
    """SIGTERM/SIGINT -> orderly serve_forever() exit. Mirrors daemon/app.py's working shutdown: the
    handler RAISES SystemExit to unwind serve_forever() from the SERVING (main) thread, then main()'s
    finally releases the socket + flock. It must NOT call server.shutdown() from that thread —
    BaseServer.shutdown() blocks until serve_forever() returns, which cannot happen from inside the
    handler, so it would DEADLOCK (socket + flock retained forever). Best-effort: signal.signal only
    works on the main thread, so a non-main-thread launch (a test, an embedded run) simply skips it."""
    def _stop(signum, _frame):
        _log.info("nelix router: shutdown requested (signal=%s)", signum)
        raise SystemExit(0)
    try:
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    except ValueError:
        pass


def main() -> None:
    router_epoch = _new_router_epoch()
    try:
        handle = establish()
    except RouterLockHeld as e:
        # One router per NELIX_HOME: the loser exits cleanly, not with a traceback.
        _log.warning("nelix router: %s; exiting", e)
        raise SystemExit(3) from None

    # ONE StartLedger and ONE registry, shared across every request thread (both are thread-safe;
    # the ledger MUST NOT be opened per-request — nelix-91y made a single shared instance safe).
    ledger = StartLedger(paths.nelix_root())
    registry = GenerationRegistry()
    start_path = StartPath(ledger, registry)
    server = make_router_server(handle.socket, handle.sock_path, start_path, registry, router_epoch)
    _install_shutdown_handlers()
    _log.info("nelix router serving on %s (epoch=%s)", handle.sock_path, router_epoch)
    try:
        server.serve_forever()
    finally:
        try:
            ledger.close()
        except Exception:
            pass
        handle.close()
        _log.info("nelix router: stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    main()
