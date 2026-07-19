"""nelix-c5o: an env_cmd resolver failure is a clean, redacted start failure at the manager layer.

Drives the REAL spawn path — a real SessionManager + real Session + real LocalLauncher run a real
failing command — so manager._spawn's cleanup + logging are exercised exactly as at spawn. The
secret-leak guard is asserted against a real obs.Logger's output (no command / stdout / stderr).
"""
import io
import json

import pytest

from nelix_store.store import Store
from nelix_store.ledger import StartLedger

from tests.conftest import EXECUTOR, OWNER, make_spec, reserve_start
from daemon.env_resolver import EnvResolveError, resolve_env_cmds
from daemon.events import EventQueue
from daemon.launchers.local import LocalLauncher
from daemon.manager import SessionManager
from daemon.obs import Logger
from daemon.drivers import get_driver

# markers a leak would carry; short + non-secret-shaped so obs.redact() would NOT mask them —
# so the assertions test the STRUCTURAL guarantee (from None), not the log redactor.
_LEAK = {"TOK": "echo LEAKOUT; echo LEAKERR 1>&2; exit 9"}


def _records(buf):
    return [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]


def _mgr(monkeypatch, tmp_path, spec, buf):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    root = tmp_path / "nelix-db"
    root.mkdir()
    store = Store(root)
    ledger = StartLedger(root)
    mgr = SessionManager({EXECUTOR: spec}, EventQueue(), store,
                         launcher_factory=lambda name: LocalLauncher(),
                         driver_factory=get_driver, concurrency_limit=3,
                         logger=Logger(level="debug", stream=buf))
    return mgr, ledger


def test_spawn_env_resolve_failure_raises_and_cleans_up(monkeypatch, tmp_path):
    buf = io.StringIO()
    spec = make_spec(command="claude", args=["--foo"], driver="claude", env_cmd=dict(_LEAK))
    m, ledger = _mgr(monkeypatch, tmp_path, spec, buf)
    with pytest.raises(EnvResolveError) as ei:
        m.start(EXECUTOR, "hi", str(tmp_path), owner_id=OWNER,
                session_id=reserve_start(ledger))
    assert ei.value.var == "TOK" and ei.value.reason == "non_zero_exit"
    assert not m._sessions               # the registered-but-unstarted session is torn down (slot freed)


def test_spawn_env_resolve_failure_logs_var_reason_without_leak(monkeypatch, tmp_path):
    buf = io.StringIO()
    spec = make_spec(command="claude", args=["--foo"], driver="claude", env_cmd=dict(_LEAK))
    m, ledger = _mgr(monkeypatch, tmp_path, spec, buf)
    with pytest.raises(EnvResolveError):
        m.start(EXECUTOR, "hi", str(tmp_path), owner_id=OWNER,
                session_id=reserve_start(ledger))
    out = buf.getvalue()
    # Redacted: neither the command string, its stdout, nor its stderr reaches any log sink.
    assert "LEAKOUT" not in out and "LEAKERR" not in out
    assert "/bin/sh" not in out and "exit 9" not in out
    # The failure IS logged, structurally: {var, reason}, and (preferred) WITHOUT an exc_info traceback.
    ssf = [r for r in _records(buf) if r.get("event") == "session_start_failed"]
    assert ssf, "session_start_failed must be logged"
    assert ssf[0].get("var") == "TOK"
    assert ssf[0].get("resolve_reason") == "non_zero_exit"
    assert "traceback" not in ssf[0]     # EnvResolveError logged without exc_info (redacted {var,reason})


def test_env_resolve_error_via_exc_info_logging_has_no_leak():
    # §7 no-leak-via-traceback: even the generic exc_info=True path (what session_start_failed used
    # for every OTHER error) cannot leak for an EnvResolveError — from None severs the chain so
    # traceback.format_exc() cannot embed the CalledProcessError's ['/bin/sh','-c',<command>] argv.
    buf = io.StringIO()
    log = Logger(level="debug", stream=buf)
    try:
        resolve_env_cmds(dict(_LEAK), {}, 5.0)
    except EnvResolveError:
        log.error("manager", "session_start_failed", session_id="s-x", exc_info=True)
    out = buf.getvalue()
    assert "session_start_failed" in out
    assert "LEAKOUT" not in out and "LEAKERR" not in out
    assert "/bin/sh" not in out and "exit 9" not in out
