import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon.config import ExecutorSpec  # noqa: E402  (after sys.path insert)

EXECUTOR = "demo"


def make_spec(**overrides):
    fields = dict(command="x", args=[], env={}, driver="claude", launcher="local")
    fields.update(overrides)
    return ExecutorSpec(**fields)
