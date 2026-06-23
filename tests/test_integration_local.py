import json, os, time, urllib.request
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NELIX_LIVE") != "1",
    reason="live test; set NELIX_LIVE=1 with a running daemon + Vault shell")

BASE = os.environ.get("NELIX_RPC", "http://127.0.0.1:8765")


def _call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"X-Nelix-Token": os.environ["NELIX_RPC_TOKEN"],
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read())


def test_local_task_reaches_decision_and_creates_file(tmp_path):
    sid = _call("POST", "/start",
                {"executor": "claude_zai",
                 "task": "create test.txt containing the word nelix"})["session_id"]
    after = 0
    deadline = time.time() + 180
    while time.time() < deadline:
        evt = _call("GET", f"/wait?after_seq={after}").get("event")
        if evt is None:
            continue
        after = evt["seq"]
        assert evt["session_id"] == sid
        if evt["kind"] == "waiting_for_user":
            _call("POST", "/respond",
                  {"session_id": sid, "event_id": evt["event_id"], "answer": "1"})
        elif evt["kind"] in ("done", "crashed"):
            break
    target = os.path.expanduser("~/tmp/nelix-skeleton/test.txt")
    assert os.path.exists(target)
    _call("POST", "/stop", {"session_id": sid})
