"""nelix-9a4.6 deliverable B: `generation_id()` derives the running generation's build id from
the interpreter path — a pure parse, so both branches (installed vs. dev/checkout) are forceable
without installing a real runtime."""
import paths
from daemon.generation import generation_id


def test_generation_id_none_for_a_dev_checkout_interpreter():
    # The interpreter actually running this test sits in the repo's own .venv, never under
    # paths.runtimes_root() (isolate_nelix_home points NELIX_HOME at a per-test scratch root, so
    # this is a real, not simulated, "not installed" case).
    assert generation_id() is None


def test_generation_id_parses_an_installed_runtime_python():
    build = "1.2.3-abc123def456"
    python = paths.runtime_python(build)     # runtimes_root()/<build>/venv/bin/python
    assert generation_id(str(python)) == build


def test_generation_id_parses_the_versioned_python_symlink_too():
    # `python -m venv` also creates bin/python3.11 alongside bin/python; sys.executable could
    # legitimately report either name depending on how the interpreter was invoked.
    build = "1.2.3-abc123def456"
    python = paths.runtime_dir(build) / "venv" / "bin" / "python3.11"
    assert generation_id(str(python)) == build


def test_generation_id_none_outside_runtimes_root():
    assert generation_id("/usr/bin/python3") is None
    assert generation_id(str(paths.nelix_root() / "sessions" / "s-1" / "python")) is None


def test_generation_id_none_for_a_malformed_path_under_runtimes_root():
    # Something else nested under runtimes_root (not build/venv/bin/python*) must not be reported
    # as a generation id — e.g. the manifest file, or a differently-shaped tree.
    root = paths.runtimes_root()
    assert generation_id(str(root / "1.2.3-abc" / "manifest.json")) is None
    assert generation_id(str(root / "1.2.3-abc" / "venv" / "bin" / "node")) is None
    assert generation_id(str(root / "1.2.3-abc")) is None
