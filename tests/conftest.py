import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon.config import ExecutorSpec  # noqa: E402  (after sys.path insert)

EXECUTOR = "demo"


def make_spec(**overrides):
    fields = dict(command="x", args=[], env={}, driver="claude", launcher="local")
    fields.update(overrides)
    return ExecutorSpec(**fields)


@pytest.fixture(autouse=True)
def isolate_nelix_home(tmp_path_factory, monkeypatch):
    """Point $NELIX_HOME at a per-test scratch root for EVERY test.

    Not hygiene theatre — it closes a live bug. MEASURED 2026-07-17, on `main` at 9b1a14e and
    BEFORE this fixture existed: a bare `pytest -q` wrote real PTY dumps into the developer's
    own home, leaving ~/.hermes/workspace/nelix/sessions/s1/{raw,capture,transcript.jsonl,
    meta.json}. It is old — the `s1` directory there dates from Jun 29 — and it hid well: the
    files are rewritten in place, and overwriting a file does not change its DIRECTORY's mtime,
    so `ls -l` on the session dir shows a June date after a run that just wrote to it. Only
    `find -newermt` sees it.

    The cause is that a default root is a REAL directory: tests that build a Session without
    naming a root (tests/test_session.py's Session("s1", ...) and friends) inherit it. The
    default has to exist for the product's sake, so the suite must override it, and no single
    test can be trusted to remember — hence autouse, at the root of the suite.

    A test that sets its own NELIX_HOME still wins: monkeypatch.setenv in the test body runs
    after this fixture. So does monkeypatch.delenv, which is how test_paths.py asserts what the
    default IS.

    tests/test_paths.py::test_the_isolation_fixture_is_in_force fails if this is deleted.
    """
    root = tmp_path_factory.mktemp("nelix-home")
    monkeypatch.setenv("NELIX_HOME", str(root))
    return root
