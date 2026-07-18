"""The minimal `nelix` CLI (Plan 3, slice 3d — spec Implementation-plans §3 "minimal CLI: ensure,
status, wait", §11 "the wake executable").

This is a CLIENT + LAUNCHER for the router, nothing more: it does not change router or daemon
behavior, and it does not ship (no pyproject `[project.scripts]` entry — router/ itself is not in
the wheel yet; that is Plan 5, which also adds the full CLI + operator verbs + deployment). It runs
from the checkout, exactly like supervisor.py / rpc_client.py / router/app.py do today.

Three subcommands, one per caller-facing need:
  * `nelix daemon ensure`  — make sure the ROUTER is serving on this NELIX_HOME (spawn it if not),
    then exit. Mirrors supervisor.ensure_running()'s SHAPE (probe -> spawn -> poll /health) but
    targets `router.app`, not `daemon.app` — the router's own establish() (router/runtime_dir.py)
    owns the exclusive lock/race, so this function only ever has to observe /health, never touch
    the lock itself.
  * `nelix daemon status`  — GET the router's owner-filtered board and print it.
  * `nelix daemon wait`    — arm the orchestration doorbell (GET /wait), print the ONE result, and
    exit. This is THE WAKE EXECUTABLE (spec §11): a background mechanism runs this once per wake,
    it never loops (the re-arming wake loop is Plan 5).

`status`/`wait` do NOT auto-ensure the router: if it is not up, they print a clear hint to run
`nelix daemon ensure` first and exit non-zero, rather than silently spawning one as a side effect
of what looks like a read.
"""
import argparse
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import paths
from rpc_client import UnixHTTPConnection
from runtime import active_python

_REPO_ROOT = Path(__file__).resolve().parent

# Mirrors supervisor.py's _HEALTH_TIMEOUT: how long `ensure` waits for a freshly spawned router to
# answer /health before giving up.
_ROUTER_HEALTH_TIMEOUT = 10.0

# The router's /wait long-polls for a fixed ~25s window (router/wait.py); give it real margin
# rather than a timeout that could fire before the router's own window does (mirrors
# bin/nelix-wait's 40s for the analogous daemon-level wait).
_WAIT_TIMEOUT = 40.0


def _router_get(path: str, timeout: float = 30):
    """GET `path` over the router's unix socket (paths.router_sock()). Returns (status,
    decoded_json_body). Raises OSError/ValueError-family exceptions on a connection failure (no
    router listening, socket gone, a non-JSON reply) — callers decide how to report that."""
    conn = UnixHTTPConnection(str(paths.router_sock()), timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


def _router_health(timeout: float = 2):
    """The router's GET /health body, or None if no router is answering on paths.router_sock() —
    the socket node is missing, refuses connections, or the reply is not a clean 200. Never
    raises: this is a PROBE (mirrors supervisor._status_body), and "not up yet" is an expected,
    common answer, not an error."""
    sock_path = paths.router_sock()
    if not sock_path.exists():
        return None
    try:
        status, body = _router_get("/health", timeout=timeout)
    except Exception:
        return None
    return body if status == 200 else None


def _router_log_path() -> Path:
    d = paths.logs_dir()
    paths.ensure_private_dir(d)
    return d / "router-launch.log"


def ensure_router(timeout: float = _ROUTER_HEALTH_TIMEOUT) -> dict:
    """Ensure a router is serving on paths.router_sock() for this NELIX_HOME, mirroring
    supervisor.ensure_running()'s shape for the ROUTER: probe -> spawn `-m router.app` -> poll
    /health up to `timeout` seconds.

    Idempotent: a healthy router already up is a no-op (never spawns a second one). The router's
    OWN establish() (router/runtime_dir.py) is what actually serializes concurrent spawns via its
    exclusive flock — if two `ensure` calls race, the loser's spawned router exits code 3
    (RouterLockHeld) while the winner keeps serving; this function does not need to inspect the
    lock itself, it only has to keep polling /health up to the ORIGINAL deadline once its own spawn
    has exited, since the winner may still be INITIALIZING (not yet serving /health) at the exact
    moment ours gave up — a single immediate re-probe would fail spuriously.

    Returns {"endpoint", "spawned", "pid", "health"} on success — `pid` is the freshly spawned
    process's pid, or None when an existing router was reused. Raises RuntimeError if no healthy
    router could be observed within `timeout` (a genuine failure, not a lost race).
    """
    health = _router_health()
    if health is not None:
        return {"endpoint": str(paths.router_sock()), "spawned": False, "pid": None,
                "health": health}

    root = paths.nelix_root()
    paths.ensure_private_dir(root)
    argv = [str(active_python() or sys.executable), "-m", "router.app"]
    env = {**os.environ, "NELIX_HOME": str(root)}
    log_path = _router_log_path()
    log = open(log_path, "ab", opener=paths.private_opener)
    try:
        proc = subprocess.Popen(
            argv, cwd=str(_REPO_ROOT), env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
    finally:
        log.close()          # parent's copy; the child inherited its own fd

    deadline = time.time() + timeout
    spawn_exited = False
    while time.time() < deadline:
        if not spawn_exited and proc.poll() is not None:
            # Exited before becoming healthy. Its own establish() exits 3 (RouterLockHeld) when a
            # concurrent ensure won the race -- the winner may still be INITIALIZING (not yet
            # serving /health) at this exact instant, so this is NOT a failure yet: keep polling
            # /health up to the ORIGINAL deadline below, so the winner's startup has real room to
            # finish and be adopted. Checked BEFORE the health probe below (same iteration), so a
            # health reply that appears in the very iteration our spawn exited is still correctly
            # attributed to the winner, never misreported as our own dead process.
            spawn_exited = True
        health = _router_health()
        if health is not None:
            if spawn_exited:
                # Our own spawn already exited; whichever router is answering /health now is the
                # lock-race winner, not us (mirrors supervisor.ensure_running's "lost the startup
                # race, reusing pid-holder" recovery).
                return {"endpoint": str(paths.router_sock()), "spawned": False, "pid": None,
                        "health": health}
            return {"endpoint": str(paths.router_sock()), "spawned": True, "pid": proc.pid,
                    "health": health}
        time.sleep(0.1)

    # Never became healthy within the deadline -- a genuine failure, not a lost race (a winner
    # becoming healthy in time would already have returned above). Make sure a stuck child cannot
    # outlive a failed ensure: escalate from a graceful terminate to a kill if it won't exit.
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    raise RuntimeError(f"nelix router did not become healthy within {timeout}s; see {log_path}")


def _cmd_ensure(args) -> int:
    try:
        result = ensure_router()
    except RuntimeError as e:
        print(f"nelix daemon ensure: {e}", file=sys.stderr)
        return 1
    health = result["health"]
    out = {"endpoint": result["endpoint"], "spawned": result["spawned"],
           "router_epoch": health.get("router_epoch"),
           "active_generation": health.get("active_generation")}
    if result["pid"] is not None:
        out["pid"] = result["pid"]
    print(json.dumps(out, ensure_ascii=False))
    return 0


_NO_ROUTER_HINT = "no router is running for this NELIX_HOME; run `nelix daemon ensure` first"

# What a router connection can genuinely fail with: no socket / connection refused / a dropped
# connection (OSError family, which covers FileNotFoundError and ConnectionRefusedError too), a
# malformed HTTP reply (http.client.HTTPException), or a non-JSON body (json.loads raises
# ValueError). Narrow on purpose -- a bug in OUR OWN code (a KeyError, a TypeError) must surface
# as a real traceback, not get relabeled "could not reach the router".
_ROUTER_CONNECTION_ERRORS = (OSError, http.client.HTTPException, ValueError)


def _cmd_status(args) -> int:
    if _router_health() is None:
        print(f"nelix daemon status: {_NO_ROUTER_HINT}", file=sys.stderr)
        return 1
    path = "/status?" + urllib.parse.urlencode({"owner_id": args.owner})
    try:
        status, body = _router_get(path, timeout=30)
    except _ROUTER_CONNECTION_ERRORS as e:
        print(f"nelix daemon status: could not reach the router: {e}", file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0 if status == 200 else 1


def _cmd_wait(args) -> int:
    if _router_health() is None:
        print(f"nelix daemon wait: {_NO_ROUTER_HINT}", file=sys.stderr)
        return 1
    params = {"owner_id": args.owner, "orchestration_id": args.orchestration}
    if args.cursor:
        params["cursor"] = args.cursor
    path = "/wait?" + urllib.parse.urlencode(params)
    try:
        status, body = _router_get(path, timeout=_WAIT_TIMEOUT)
    except _ROUTER_CONNECTION_ERRORS as e:
        print(f"nelix daemon wait: could not reach the router: {e}", file=sys.stderr)
        return 1
    # One line, machine-readable (spec §11: a harness consumes this, not a human reading prose).
    print(json.dumps(body, ensure_ascii=False))
    return 0 if status == 200 else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nelix",
        description="Minimal nelix CLI: ensure the router is running, read its board, "
                    "arm the orchestration wait doorbell (spec Implementation-plans §3, §11).")
    top = parser.add_subparsers(dest="command", required=True)

    daemon = top.add_parser("daemon", help="router lifecycle, board reads, orchestration wait")
    sub = daemon.add_subparsers(dest="daemon_command", required=True)

    p_ensure = sub.add_parser(
        "ensure", help="ensure the router is running for this NELIX_HOME (idempotent)")
    p_ensure.set_defaults(func=_cmd_ensure)

    p_status = sub.add_parser("status", help="print the router's owner-filtered board")
    p_status.add_argument("--owner", required=True, help="the owner_id to filter the board by")
    p_status.set_defaults(func=_cmd_status)

    p_wait = sub.add_parser(
        "wait", help="arm the orchestration doorbell and print the one-shot result, then exit")
    p_wait.add_argument("--owner", required=True, help="the owner_id the wait is scoped to")
    p_wait.add_argument("--orchestration", required=True,
                        help="the orchestration_id to wait on")
    p_wait.add_argument("--cursor", default=None,
                        help="opaque cursor token from a prior wait/status reply; "
                             "omit to arm from now")
    p_wait.set_defaults(func=_cmd_wait)

    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
