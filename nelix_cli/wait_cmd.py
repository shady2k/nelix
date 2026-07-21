"""`nelix wait` — THE WAKE EXECUTABLE. It exits on the FIRST thing worth waking for, and the host's
background mechanism re-invokes the model when it exits.

It is one-shot to its CALLER and a loop INSIDE: the router's /wait window is a fixed ~25s and is
deliberately not per-call tunable (router/wait.py), so a single window would wake the model every
25 seconds forever. This process therefore re-polls window after window — carrying the cursor
forward each time so nothing is missed across the seam — until an event, a resync, an empty
orchestration, or its own deadline. That deadline is a real wake too: it prints "no events" and the
re-arm line, which doubles as a liveness check on the whole loop.
"""
import signal
import time
import urllib.parse

from nelix_cli import daemon_cmds, doorbell, rpc
from nelix_cli.envelope import EXIT_OK, EXIT_UNAVAILABLE, ROUTER_ERRORS, emit_error

# Per-call ceiling, comfortably above the router's own ~25s window (mirrors bin/nelix-wait's 45s).
_WINDOW_TIMEOUT = 45.0
# Floor between windows. The router answers some replies instantly; without this a degenerate reply
# would spin the loop at full speed.
_MIN_INTERVAL = 1.0


def _poll(owner_id: str, orchestration_id: str, cursor) -> dict:
    params = {"owner_id": owner_id, "orchestration_id": orchestration_id}
    if cursor:
        params["cursor"] = cursor
    _status, body = rpc.client_for(owner_id)._call(
        "GET", "/wait?" + urllib.parse.urlencode(params), timeout=_WINDOW_TIMEOUT)
    return body if isinstance(body, dict) else {}


def _signal_to_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt()


def cmd_wait(args) -> int:
    if daemon_cmds._router_health() is None:
        return emit_error("router_unavailable",
                          "no router is running for this NELIX_HOME; "
                          "run `nelix daemon ensure` first",
                          exit_class=EXIT_UNAVAILABLE)
    signal.signal(signal.SIGTERM, _signal_to_keyboard_interrupt)
    deadline = time.monotonic() + args.timeout
    cursor = args.cursor
    while True:
        started = time.monotonic()
        try:
            body = _poll(args.owner, args.orchestration, cursor)
        except ROUTER_ERRORS as e:
            return emit_error("router_call_failed", f"wait failed: {e}",
                              exit_class=EXIT_UNAVAILABLE)
        except KeyboardInterrupt:
            classified = {"reason": "none", "cursor": cursor}
            print(doorbell.render(classified, owner=args.owner,
                                  orchestration=args.orchestration))
            return EXIT_OK
        classified = doorbell.classify(body)
        if classified["cursor"]:
            cursor = classified["cursor"]           # carry it across the window seam
        if classified["reason"] != "none" or time.monotonic() >= deadline:
            classified["cursor"] = cursor
            print(doorbell.render(classified, owner=args.owner,
                                  orchestration=args.orchestration))
            return EXIT_OK
        elapsed = time.monotonic() - started
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)


def add_parser(top) -> None:
    p = top.add_parser("wait", help="wait for the first event worth waking for, print it, exit")
    p.add_argument("--owner", required=True)
    p.add_argument("--orchestration", required=True)
    p.add_argument("--cursor", default=None,
                   help="cursor from the previous doorbell; omit to arm from now")
    p.add_argument("--timeout", type=float, default=1800.0,
                   help="give up after this many seconds and wake with 'no events' (default 1800)")
    p.set_defaults(func=cmd_wait)
