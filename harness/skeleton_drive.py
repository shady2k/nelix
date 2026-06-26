"""Phase-1 harness: drive the daemon end-to-end WITHOUT Hermes tools.

Usage: .venv/bin/python harness/skeleton_drive.py "create hello.txt with the word nelix" [executor] [cwd]
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("NELIX_RPC", "http://127.0.0.1:8765")
TOKEN = os.environ["NELIX_RPC_TOKEN"]


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"X-Nelix-Token": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def main():
    if len(sys.argv) < 2:
        raise SystemExit('usage: skeleton_drive.py "<task>" [executor] [cwd]')
    task = sys.argv[1]
    executor = sys.argv[2] if len(sys.argv) > 2 else "claude"
    cwd = sys.argv[3] if len(sys.argv) > 3 else os.getcwd()
    start = call("POST", "/start", {"executor": executor, "task": task, "cwd": cwd})
    sid = start["session_id"]
    after = int(start.get("next_after_seq", 0))
    print(f"[harness] started {sid}: {task}")
    while True:
        evt = call("GET", f"/wait?after_seq={after}&session_id={sid}").get("event")
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
        call("POST", "/respond", {"session_id": sid,
                                  "answer": input("[harness] answer > ").strip()})


if __name__ == "__main__":
    main()
