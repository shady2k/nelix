import os, sys, time
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import supervisor
from daemon.transport import Transport
from rpc_client import RpcClient

NELIX_LIVE = os.environ.get("NELIX_LIVE")

pytestmark = pytest.mark.skipif(
    NELIX_LIVE != "1",
    reason="live test; set NELIX_LIVE=1 with a running daemon + Vault shell")


def _call(method, path, body=None):
    return RpcClient(supervisor.endpoint())._call(method, path, body)[1]


def test_local_task_reaches_decision_and_creates_file(tmp_path):
    sid = _call("POST", "/start",
                {"executor": os.environ.get("NELIX_EXECUTOR", "demo"),
                 "task": "create test.txt containing the word nelix",
                 "cwd": str(tmp_path)})["session_id"]
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
                  {"session_id": sid, "answer": "1"})
        elif evt["kind"] in ("done", "crashed"):
            break
    target = os.path.join(str(tmp_path), "test.txt")   # lands in the per-session cwd, not a skeleton dir
    assert os.path.exists(target)
    _call("POST", "/stop", {"session_id": sid})


def test_supervisor_spawns_daemon_and_runs(tmp_path, monkeypatch):
    import registry
    # operator must point HERMES_HOME at a profile whose nelix.toml has NELIX_EXECUTOR
    transport = supervisor.ensure_running()
    try:
        sid = RpcClient(transport).start(os.environ["NELIX_EXECUTOR"],
                                         "create test.txt containing the word nelix",
                                         str(tmp_path))["session_id"]
        assert sid
    finally:
        supervisor.teardown()
