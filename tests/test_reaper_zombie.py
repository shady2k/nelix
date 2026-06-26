import os
import sys

import pytest

from daemon.reaper import ProcessInspector


@pytest.mark.skipif(sys.platform != "linux", reason="Z-state read is /proc-only")
def test_zombie_is_dead(tmp_path):
    # A child that exits but is not yet waited becomes a zombie: kill(pid,0) still
    # succeeds, but it must be reported dead.
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    insp = ProcessInspector()
    # Do NOT waitpid yet -> pid is a zombie. Poll briefly for the Z state.
    import time
    for _ in range(50):
        if not insp.is_alive(pid):
            break
        time.sleep(0.02)
    assert insp.is_alive(pid) is False
    os.waitpid(pid, 0)


def test_live_process_is_alive():
    insp = ProcessInspector()
    assert insp.is_alive(os.getpid()) is True
