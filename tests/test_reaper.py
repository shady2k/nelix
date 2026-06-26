import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon import reaper  # noqa: E402


def test_inspector_reads_live_process_facts():
    insp = reaper.ProcessInspector()
    me = os.getpid()
    assert insp.is_alive(me) is True
    assert insp.is_alive(2_000_000_000) is False        # implausible pid
    assert insp.pgid(me) == os.getpgrp()
    fp = insp.start_fingerprint(me)
    assert isinstance(fp, str) and fp != ""
    assert insp.start_fingerprint(me) == fp              # stable for the same process


def test_killer_signals_a_real_group():
    # spawn a child in its own session/group, then killpg it via the real killer.
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:                                          # child
        os.close(r)
        os.setsid()
        os.close(w)                                      # signal parent: setsid done
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        while True:
            signal.pause()
    os.close(w)
    os.read(r, 1)                                        # block until child closed w (post-setsid)
    os.close(r)
    insp, killer = reaper.ProcessInspector(), reaper.ProcessKiller()
    pgid = insp.pgid(pid)
    killer.killpg(pgid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status)


def test_record_read_forget_roundtrip(tmp_path):
    import paths
    sd = tmp_path / "s-aaaaaaaa"; sd.mkdir()
    rec = {"sid": "s-aaaaaaaa", "daemon_pid": 10, "daemon_fingerprint": "d1",
           "pid": 20, "child_fingerprint": "c1", "pgid": 20, "argv": ["claude"]}
    reaper.record_child(sd, rec)
    assert paths.child_record(sd).exists()
    assert oct(paths.child_record(sd).stat().st_mode)[-3:] == "600"
    assert reaper.read_child(sd) == rec
    reaper.forget_child(sd)
    assert reaper.read_child(sd) is None
    reaper.forget_child(sd)                       # idempotent


def test_read_child_quarantines_garbage(tmp_path):
    import paths
    sd = tmp_path / "s-bbbbbbbb"; sd.mkdir()
    paths.child_record(sd).write_text("{not json")
    assert reaper.read_child(sd) is None
    assert (sd / "child.json.bad").exists()
    assert not paths.child_record(sd).exists()


class _RecKiller:
    def __init__(self): self.calls = []
    def killpg(self, pgid, sig): self.calls.append((pgid, sig))


class _ScriptInspector:
    """is_alive returns the queued booleans in order (then the last forever)."""
    def __init__(self, alive_seq): self.alive_seq = list(alive_seq); self.i = -1
    def is_alive(self, pid):
        self.i = min(self.i + 1, len(self.alive_seq) - 1)
        return self.alive_seq[self.i]


def test_kill_group_terminates_then_kills_after_grace():
    import signal
    killer = _RecKiller()
    insp = _ScriptInspector([True, True])          # never dies on TERM -> escalates
    issued = reaper.kill_group(insp, killer, leader_pid=20, pgid=20, grace=0.05, poll=0.01)
    assert issued is True
    assert killer.calls[0] == (20, signal.SIGTERM)
    assert killer.calls[-1] == (20, signal.SIGKILL)


def test_kill_group_skips_when_pgid_missing():
    killer = _RecKiller()
    insp = _ScriptInspector([True])
    assert reaper.kill_group(insp, killer, leader_pid=20, pgid=None, grace=0.05) is False
    assert killer.calls == []


def test_kill_group_no_sigkill_if_terminated_in_grace():
    import signal
    killer = _RecKiller()
    insp = _ScriptInspector([False])               # gone right after TERM
    reaper.kill_group(insp, killer, leader_pid=20, pgid=20, grace=0.2, poll=0.01)
    assert (20, signal.SIGTERM) in killer.calls
    assert (20, signal.SIGKILL) not in killer.calls
