"""nelix-9k0: daemon-level /start model override, asserted against the REAL spawn/argv path.

A real SessionManager (real ClaudeDriver via get_driver, a real Session) drives a capturing
launcher that records the exact `spec.argv()` handed to it — which is byte-for-byte what the
LocalLauncher passes the broker. No fabricated PTY frames: the child is reported already-exited
so the monitor finalizes cleanly.
"""
import io
import json
import threading

import pytest

from tests.conftest import EXECUTOR, OWNER, make_spec, reserve_start
from daemon.drivers import get_driver
from daemon.events import EventQueue
from daemon.manager import SessionManager
from daemon.obs import Logger
from daemon.rpc_server import make_server
from daemon.transport import Transport
from daemon.launchers.base import LeaderStatus


class _DeadHandle:
    """A PTY leader that has already exited(0): the monitor observes the exit and finalizes at once."""
    def __init__(self): self.writes = []
    def pump(self, timeout=0.1): return False
    def render(self): return ""
    def is_alive(self): return False
    def exit_code(self): return 0
    def write(self, data, timeout=None, drain_output=False): self.writes.append(data)
    def finalize(self): pass
    def leader_pid(self): return 4242
    def leader_pgid(self): return 4242
    def assert_leader_is_group_leader(self): pass
    def leader_status(self):
        return LeaderStatus(alive=False, exit_code=0, signal=None, status_available=True)
    def close(self): pass


class _CapturingLauncher:
    """Records the argv of every spec it is asked to spawn (the real broker argv)."""
    def __init__(self, captured): self._captured = captured
    def start(self, spec, cwd, cols=120, rows=40, dialog=None, transcript=None,
              *, session_id=None, hook_secret=None):
        self._captured.append(list(spec.argv()))
        return _DeadHandle()
    def stop(self, handle): handle.close()


def _serve(monkeypatch, tmp_path, store_and_ledger, *, spec=None, driver_factory=None, port=8801):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    store, ledger = store_and_ledger
    captured = []
    buf = io.StringIO()
    mgr = SessionManager({EXECUTOR: spec or make_spec(command="claude", args=["--foo"],
                                                      driver="claude")},
                         EventQueue(), store,
                         launcher_factory=lambda name: _CapturingLauncher(captured),
                         driver_factory=driver_factory or get_driver,
                         concurrency_limit=3, logger=Logger(level="debug", stream=buf))
    srv = make_server(mgr, Transport.tcp("127.0.0.1", port, "t"),
                      logger=Logger(level="debug", stream=buf))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, mgr, captured, buf, ledger


def _req(port, body):
    import urllib.error, urllib.request
    # /start is owner-gated (daemon/owner.py). These tests are about env/model resolution, not
    # ownership, so the helper supplies the owner rather than every call site restating it.
    body = {"owner_id": OWNER, **body}
    data = json.dumps(body).encode()
    r = urllib.request.Request(f"http://127.0.0.1:{port}/start", data=data, method="POST",
                               headers={"X-Nelix-Token": "t"})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_daemon_start_with_model_reflects_in_spawned_argv_and_log(monkeypatch, tmp_path, store_and_ledger):
    srv, mgr, captured, buf, ledger = _serve(monkeypatch, tmp_path, store_and_ledger, port=8801)
    try:
        st, b = _req(8801, {"executor": EXECUTOR, "task": "hi", "cwd": str(tmp_path),
                            "model": "haiku", "session_id": reserve_start(ledger)})
        assert st == 200 and b["session_id"]
        assert captured == [["claude", "--foo", "--model", "haiku"]]      # spawned argv reflects it
        spawned = [json.loads(l) for l in buf.getvalue().splitlines()
                   if l.strip() and json.loads(l).get("event") == "executor_spawned"]
        assert spawned and spawned[0]["argv_redacted"][-2:] == ["--model", "haiku"]   # lifecycle log shows it
    finally:
        mgr.stop_all(); srv.shutdown()


def test_daemon_start_without_model_argv_unchanged(monkeypatch, tmp_path, store_and_ledger):
    srv, mgr, captured, buf, ledger = _serve(monkeypatch, tmp_path, store_and_ledger, port=8802)
    try:
        st, b = _req(8802, {"executor": EXECUTOR, "task": "hi", "cwd": str(tmp_path),
                            "session_id": reserve_start(ledger)})
        assert st == 200
        assert captured == [["claude", "--foo"]]                          # no --model injected
    finally:
        mgr.stop_all(); srv.shutdown()


def test_daemon_start_bad_shape_returns_400(monkeypatch, tmp_path, store_and_ledger):
    srv, mgr, captured, buf, ledger = _serve(monkeypatch, tmp_path, store_and_ledger, port=8803)
    try:
        st, b = _req(8803, {"executor": EXECUTOR, "task": "hi", "cwd": str(tmp_path),
                            "model": "bad\nshape", "session_id": reserve_start(ledger)})
        assert st == 400 and "error" in b
        assert captured == []                                             # no spawn
    finally:
        mgr.stop_all(); srv.shutdown()


class _NoModelDriver:
    def observe(self, *a, **k): pass


def test_daemon_start_unsupported_driver_returns_400(monkeypatch, tmp_path, store_and_ledger):
    srv, mgr, captured, buf, ledger = _serve(monkeypatch, tmp_path, store_and_ledger, port=8804,
                                     driver_factory=lambda name: _NoModelDriver())
    try:
        st, b = _req(8804, {"executor": EXECUTOR, "task": "hi", "cwd": str(tmp_path),
                            "model": "haiku", "session_id": reserve_start(ledger)})
        assert st == 400 and "error" in b
        assert captured == []
    finally:
        mgr.stop_all(); srv.shutdown()
