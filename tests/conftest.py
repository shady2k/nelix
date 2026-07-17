import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon.config import ExecutorSpec  # noqa: E402  (after sys.path insert)

EXECUTOR = "demo"

# The owner most tests drive as. `owner_id` is required on every caller-facing manager/RPC call
# (daemon/owner.py), so a test that omits it is a TypeError, not a silent pass — which is the
# point of the parameter having no default. Tests that care about ISOLATION rather than merely
# satisfying the signature use their own two owners: see tests/test_owner_isolation.py.
OWNER = "test-owner"


def own(session_id, owner_id=OWNER):
    """Write the durable owner record a real `manager.start()` would have written.

    For tests that put a Session into `mgr._sessions` BY HAND. Those bypass start(), so they
    bypass the owner write, and every owner-filtered read then hides the session — which is
    fail-closed working correctly, not an obstacle to route around. This restores the state a
    real start leaves behind; it does not weaken the gate. Returns the session_id so it can wrap
    an injection inline.
    """
    from daemon import owner as _owner
    import paths as _paths
    _owner.write(_paths.sessions_root() / session_id, owner_id)
    return session_id


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
