"""Phase-1 harness: drive the daemon end-to-end WITHOUT Hermes tools.

Usage: .venv/bin/python harness/skeleton_drive.py "create hello.txt with the word nelix" [executor] [cwd]

$NELIX_OWNER_ID names the owner this harness drives as (default: "skeleton-drive"). It is A
HARNESS, so it gets ONE owner for its whole run and only ever sees its own sessions — which is
the point: run this alongside another harness on the same daemon and neither sees the other's
board. It is not a credential; the daemon cannot tell one local caller from another.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths
from daemon.transport import Transport
from rpc_client import RpcClient

OWNER_ID = os.environ.get("NELIX_OWNER_ID") or "skeleton-drive"


def _client():
    with open(paths.state_file()) as f:
        return RpcClient(Transport.from_state(json.load(f)), OWNER_ID)


def main():
    if len(sys.argv) < 2:
        raise SystemExit('usage: skeleton_drive.py "<task>" [executor] [cwd]')
    task = sys.argv[1]
    executor = sys.argv[2] if len(sys.argv) > 2 else "claude"
    cwd = sys.argv[3] if len(sys.argv) > 3 else os.getcwd()
    start = _client().start(executor, task, cwd)
    sid = start["session_id"]
    after = int(start.get("next_after_seq", 0))
    print(f"[harness] started {sid} as owner {OWNER_ID}: {task}")
    while True:
        body = _client().wait(sid, after_seq=after, timeout=40)
        if body.get("error"):
            # Unknown session, or not ours: this wait can never wake, so re-issuing would be a
            # hot loop (the `continue` below is only correct for a poll that actually blocked).
            print(f"[harness] {sid}: {body['error']} — stopping.")
            return
        evt = body.get("event")
        if evt is None:
            continue
        after = evt["seq"]
        print(f"\n[event #{evt['seq']} {evt['kind']}] state={evt.get('state')}\n{evt.get('summary', '')}\n")
        if evt["kind"] in ("done", "crashed", "delivery_failed"):
            print(f"[harness] terminal event: {evt['kind']} — stopping.")
            return
        if not evt.get("requires_response"):
            continue
        # respond binds to the session's current pending decision — no event_id needed.
        _client().respond(sid, input("[harness] answer > ").strip())


if __name__ == "__main__":
    main()
