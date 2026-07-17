"""nelix-3rm (router slice 3c.1): the router keys a generation EPOCH on the daemon's process
incarnation so that a daemon RESTART (new pid, or a reused pid with a new start time) yields a NEW
epoch. supervisor.incarnation() surfaces that identity — (pid, start_fingerprint) — from the same
.active.json state endpoint() already trusts, with the same liveness + fingerprint gate."""
import importlib
import os

import paths
import supervisor
from daemon import reaper
from daemon.transport import Transport


def test_incarnation_is_none_when_no_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    assert supervisor.incarnation() is None


def test_incarnation_reports_pid_and_fingerprint_of_live_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    paths.ensure_private_dir(paths.nelix_root())
    supervisor._write_state(os.getpid(), Transport.unix(str(paths.rpc_sock())))
    inc = supervisor.incarnation()
    assert inc is not None
    assert inc["pid"] == os.getpid()
    assert inc["start_fingerprint"] == reaper.ProcessInspector().start_fingerprint(os.getpid())


def test_incarnation_rejects_a_reused_pid_whose_fingerprint_differs(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    importlib.reload(paths); importlib.reload(supervisor)
    paths.ensure_private_dir(paths.nelix_root())
    supervisor._write_state(os.getpid(), Transport.unix(str(paths.rpc_sock())))
    # The pid is alive (our own) but reports a DIFFERENT start fingerprint than the one recorded:
    # a recycled pid must not masquerade as the same daemon incarnation.
    monkeypatch.setattr(reaper.ProcessInspector, "start_fingerprint",
                        lambda self, pid: "different-fingerprint")
    assert supervisor.incarnation() is None
