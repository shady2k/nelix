"""nelix-c5o: /start maps an env_cmd resolver failure to a distinct, redacted 502.

Drives the REAL path — a real SessionManager + Session + LocalLauncher run a real failing command,
so the EnvResolveError propagates through manager._spawn to the /start handler exactly as at spawn.
502 (not the generic 409): an upstream resolver/secret-backend failure, daemon healthy, relayable.
"""
import io
import json
import threading

from tests.conftest import EXECUTOR, OWNER, make_spec, reserve_start
from daemon.events import EventQueue
from daemon.launchers.local import LocalLauncher
from daemon.manager import SessionManager
from daemon.obs import Logger
from daemon.rpc_server import make_server
from daemon.transport import Transport
from daemon.drivers import get_driver


def _serve(monkeypatch, tmp_path, store_and_ledger, spec, port):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    store, ledger = store_and_ledger
    buf = io.StringIO()
    mgr = SessionManager({EXECUTOR: spec}, EventQueue(), store,
                         launcher_factory=lambda name: LocalLauncher(),
                         driver_factory=get_driver, concurrency_limit=3,
                         logger=Logger(level="debug", stream=buf))
    srv = make_server(mgr, Transport.tcp("127.0.0.1", port, "t"),
                      logger=Logger(level="debug", stream=buf))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, mgr, buf, ledger


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


def test_env_cmd_failure_returns_502_redacted(monkeypatch, tmp_path, store_and_ledger):
    spec = make_spec(command="claude", args=["--foo"], driver="claude",
                     env_cmd={"TOK": "echo LEAKOUT; echo LEAKERR 1>&2; exit 5"})
    srv, mgr, buf, ledger = _serve(monkeypatch, tmp_path, store_and_ledger, spec, port=8831)
    try:
        st, b = _req(8831, {"executor": EXECUTOR, "task": "hi", "cwd": str(tmp_path),
                             "session_id": reserve_start(ledger)})
        assert st == 502                                   # distinct from the generic 409
        assert "error" in b
        assert "TOK" in b["error"]                         # names the VAR (relayable)
        assert "LEAKOUT" not in b["error"] and "LEAKERR" not in b["error"]   # not the stdout/stderr
        assert "echo" not in b["error"] and "/bin/sh" not in b["error"]      # not the command
        assert "LEAKOUT" not in buf.getvalue()             # nor any log sink
    finally:
        mgr.stop_all(); srv.shutdown()


def test_timeout_env_cmd_returns_502(monkeypatch, tmp_path, store_and_ledger):
    spec = make_spec(command="claude", args=["--foo"], driver="claude",
                     env_cmd={"SLOW": "sleep 5"}, env_cmd_timeout_seconds=0.2)
    srv, mgr, buf, ledger = _serve(monkeypatch, tmp_path, store_and_ledger, spec, port=8832)
    try:
        st, b = _req(8832, {"executor": EXECUTOR, "task": "hi", "cwd": str(tmp_path),
                             "session_id": reserve_start(ledger)})
        assert st == 502
        assert "SLOW" in b["error"]
    finally:
        mgr.stop_all(); srv.shutdown()
