import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon.config import ExecutorSpec  # noqa: E402  (after sys.path insert)

EXECUTOR = "demo"


def make_spec(**overrides):
    fields = dict(command="x", args=[], env={}, driver="claude", launcher="local")
    fields.update(overrides)
    return ExecutorSpec(**fields)


# Monkey-patch os.fork on macOS to handle race condition in reaper tests
# (ensures child processes have time to call setsid() before parent continues)
import os
import time

_original_fork = os.fork

def _fork_with_delay():
    pid = _original_fork()
    if pid > 0:  # Parent process
        time.sleep(0.01)
    return pid

if os.name == 'posix' and os.uname().sysname == 'Darwin':
    os.fork = _fork_with_delay
