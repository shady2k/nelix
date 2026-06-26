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
    pid = os.fork()
    if pid == 0:                                          # child
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        while True:
            signal.pause()
    insp, killer = reaper.ProcessInspector(), reaper.ProcessKiller()
    pgid = insp.pgid(pid)
    killer.killpg(pgid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status)
