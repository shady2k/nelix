"""Phase-1 harness: drive the daemon end-to-end WITHOUT Hermes tools.

Usage: .venv/bin/python harness/skeleton_drive.py "create hello.txt with the word nelix" [executor] [cwd]

$NELIX_OWNER_ID names the owner this harness drives as (default: "skeleton-drive"). It is A
HARNESS, so it gets ONE owner for its whole run and only ever sees its own sessions — which is
the point: run this alongside another harness on the same daemon and neither sees the other's
board. It is not a credential; the daemon cannot tell one local caller from another.

S1c-2 / H14: routes through the router, using the orchestration /wait
(owner_id + orchestration_id + vector cursor).
"""

import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths
from daemon.transport import Transport
from rpc_client import RpcClient

OWNER_ID = os.environ.get("NELIX_OWNER_ID") or "skeleton-drive"


def _client():
    # S1c-2: route through the router's public socket.
    sock_path = str(paths.router_sock())
    return RpcClient(Transport.unix(sock_path), OWNER_ID)


def main():
    if len(sys.argv) < 2:
        raise SystemExit('usage: skeleton_drive.py "<task>" [executor] [cwd]')
    task = sys.argv[1]
    executor = sys.argv[2] if len(sys.argv) > 2 else "claude"
    cwd = sys.argv[3] if len(sys.argv) > 3 else os.getcwd()
    start = _client().start(executor, task, cwd)
    sid = start["session_id"]
    orchestration_id = start.get("orchestration_id", sid)
    after = int(start.get("next_after_seq", 0))
    print(f"[harness] started {sid} as owner {OWNER_ID}: {task}")

    # H14: Use the router's orchestration /wait with owner_id + orchestration_id + cursor.
    cursor = None
    while True:
        params = {"owner_id": OWNER_ID, "orchestration_id": orchestration_id}
        if cursor:
            params["cursor"] = cursor
        path = "/wait?" + urllib.parse.urlencode(params)
        _, body = _client()._call("GET", path, timeout=40)

        if not isinstance(body, dict):
            time.sleep(1)
            continue

        if body.get("error"):
            print(f"[harness] {sid}: {body['error']} — stopping.")
            return

        # H14: Save+reuse the router vector cursor from every reply.
        if body.get("cursor"):
            cursor = body["cursor"]

        # Resync markers — refetch cursor from router's board endpoint.
        if body.get("cursor_expired") or body.get("board_changed"):
            _, board = _client()._call("GET", f"/status?owner_id={OWNER_ID}")
            if isinstance(board, dict) and board.get("cursor"):
                cursor = board["cursor"]
            continue

        evt = body.get("event")
        if evt is None:
            # Timeout — keep waiting
            continue

        seq = evt.get("seq", 0)
        kind = evt.get("kind", "unknown")
        print(f"\n[event #{seq} {kind}] state={evt.get('state')}\n{evt.get('summary', '')}\n")

        if kind in ("done", "crashed", "delivery_failed"):
            print(f"[harness] terminal event: {kind} — stopping.")
            return

        if not evt.get("requires_response"):
            continue

        _client().respond(sid, input("[harness] answer > ").strip())


if __name__ == "__main__":
    main()
