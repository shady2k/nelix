"""Executor-facing wrapper: post a non-waking progress NOTE to the orchestrator. POSTs
`POST /message/<sid>` (daemon/rpc_server.py's `_dispatch_message`) over the SAME env + transport
the hook curl already uses (daemon/hook_settings.py:19-23): NELIX_HOOK_SOCK / NELIX_HOOK_SECRET /
NELIX_SESSION, injected at spawn."""
import argparse
import json
import os
import subprocess
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", required=True)
    ap.add_argument("--details", default=None)
    a = ap.parse_args()

    body = {"kind": "note", "summary": a.summary}
    if a.details:
        body["details"] = a.details

    sock = os.environ.get("NELIX_HOOK_SOCK")
    secret = os.environ.get("NELIX_HOOK_SECRET")
    sid = os.environ.get("NELIX_SESSION")
    if not (sock and secret and sid):
        print("nelix-note: not running under a nelix session", file=sys.stderr)
        return 2

    r = subprocess.run(
        ["curl", "-s", "--max-time", "5", "--unix-socket", sock,
         "-H", f"X-Nelix-Hook-Secret: {secret}",
         "http://x/message/" + sid, "-d", json.dumps(body), "-w", "\n%{http_code}"],
        capture_output=True, text=True)
    out, _, code = r.stdout.rpartition("\n")
    print(out)
    return 0 if code == "200" else 1


if __name__ == "__main__":
    sys.exit(main())
