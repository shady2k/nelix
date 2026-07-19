"""The lock/pyproject drift guard.

requirements-runtime.lock is what every installed generation is frozen from, and it is COMPILED
from pyproject.toml's [project.dependencies] — so the two can disagree only if someone edits one
and not the other. That is not hypothetical: this file's predecessor, requirements-daemon.lock, was
compiled from a hand-kept requirements-daemon.in and spent months pinning ptyprocess as a daemon
dep that nothing under daemon/ has ever imported [nelix-9a4.1]. Nothing noticed, because nothing
compared them.
"""
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "requirements-runtime.lock"
PYPROJECT = REPO / "pyproject.toml"


def _pyproject_deps():
    with open(PYPROJECT, "rb") as f:
        return sorted(tomllib.load(f)["project"]["dependencies"])


def _locked():
    """{name: version} for every pin in the lock. Hash lines are continuations; comments are not
    requirements."""
    out = {}
    for line in LOCK.read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9._-]+)==([^\s\\]+)", line.strip())
        if m:
            out[m.group(1).lower().replace("_", "-")] = m.group(2)
    return out


def test_lock_pins_exactly_the_declared_runtime_deps():
    """Same dependency SET both sides. A dep added to pyproject and not compiled in would be
    installed into a generation unpinned and unhashed — or not at all.
    nelix-9a4.4: nelix_store and nelix_contracts are LOCAL packages (not third-party), so they
    are built + installed alongside the core wheel rather than pinned in the lock file."""
    declared = {}
    for spec in _pyproject_deps():
        m = re.match(r"^([A-Za-z0-9._-]+)==([^\s;]+)$", spec)
        assert m, f"{spec!r} in pyproject is not an exact pin; the runtime closure must be frozen"
        name = m.group(1).lower().replace("_", "-")
        if name in ("nelix-store", "nelix-contracts"):
            continue  # local packages, not in the lock
        declared[name] = m.group(2)
    assert set(_locked()) >= set(declared), (
        f"declared in pyproject but not locked: {set(declared) - set(_locked())} — recompile: "
        f"uv pip compile pyproject.toml --generate-hashes --no-header --python-version 3.11 "
        f"-o requirements-runtime.lock")
    for name, version in declared.items():
        assert _locked()[name] == version, (
            f"{name} is =={version} in pyproject but =={_locked()[name]} in the lock")


def test_every_locked_pin_carries_hashes():
    """--require-hashes is the whole point: an unhashed pin lets a redirected index substitute an
    artifact into a generation. uv omits hashes silently if --generate-hashes is forgotten.
    Local packages (nelix_store, nelix_contracts) are exempt — they ship with the core wheel."""
    body = LOCK.read_text()
    for name in _locked():
        if name in ("nelix-store", "nelix-contracts"):
            continue
        block = body.split(f"{name}==", 1)[1]
        block = block.split("\n\n", 1)[0]
        assert "--hash=sha256:" in block, f"{name} is pinned without hashes in {LOCK.name}"


def test_lock_does_not_carry_dev_or_retired_deps():
    """The generation gets the RUNTIME closure and nothing else. pytest/pyyaml are dev extras, and
    ptyprocess is the retired claim this guard exists because of — the PTY is stdlib os.openpty +
    os.login_tty, so a ptyprocess pin here would mean the lock had drifted back to fiction."""
    for absent in ("pytest", "pyyaml", "ptyprocess"):
        assert absent not in _locked(), (
            f"{absent} is pinned in the runtime lock; it is not a runtime dep of the core")
