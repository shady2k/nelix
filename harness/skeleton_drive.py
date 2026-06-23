"""Phase-1 harness: drive the daemon end-to-end WITHOUT Hermes tools.

Usage: .venv/bin/python harness/skeleton_drive.py "create hello.txt with the word nelix"
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
        raise SystemExit('usage: skeleton_drive.py "<task>"')
    call("POST", "/start", {"task": sys.argv[1]})
    print(f"[harness] started: {sys.argv[1]}")
    after = 0
    while True:
        evt = call("GET", f"/wait?after_seq={after}").get("event")
        if evt is None:
            continue
        after = evt["seq"]
        print(f"\n[event #{evt['seq']} {evt['kind']}] state={evt['state']}\n{evt['summary']}\n")
        if evt["kind"] in ("done", "crashed"):
            print(f"[harness] terminal event: {evt['kind']} — stopping.")
            return
        call("POST", "/respond", {"event_id": evt["event_id"],
                                  "answer": input("[harness] answer > ").strip()})


if __name__ == "__main__":
    main()
