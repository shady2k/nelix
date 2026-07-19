"""Runtime identity, commit semantics and selection [nelix-9a4.2].

These are the fast half: build ids, what counts as installed, and what the supervisor does with an
active runtime. They use FAKE runtime directories — a manifest and a stub interpreter — because
none of this needs a real 78MB install to be wrong. tests/test_real_runtime.py builds the real
thing and is where "it actually runs" is settled.
"""
import importlib
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402
import runtime  # noqa: E402


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    monkeypatch.delenv("NELIX_RUNTIME", raising=False)
    importlib.reload(paths)
    importlib.reload(runtime)
    yield


def _wheel(path, *, version="0.1.0", payload=b"x", stamp=(2026, 7, 17, 10, 0, 0)):
    """A minimal wheel-NAMED zip. build_id only reads the filename's version field and the member
    payloads, so nothing here needs to be installable.

    `stamp` is the member timestamp, and it is a parameter rather than a default because the
    default is a TRAP: `writestr` with a bare name stamps every member 1980-01-01, so two wheels
    built from different sources at different times come out byte-identical and any assertion about
    file-vs-payload hashing passes vacuously. A real zip carries each member's SOURCE mtime.
    """
    p = Path(path) / f"nelix_core-{version}-py3-none-any.whl"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(zipfile.ZipInfo("daemon/__init__.py", date_time=stamp), payload)
    return p


def _fake_runtime(build, *, complete=True):
    """A runtime directory with a stub interpreter, committed iff `complete`."""
    py = paths.runtime_python(build)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\nexec /usr/bin/true\n")
    py.chmod(0o755)
    if complete:
        paths.runtime_manifest(build).write_text(json.dumps({"build_id": build}))
    return build


# ---------------------------------------------------------------- identity

def test_build_id_is_stable_across_rebuilds_of_identical_sources(tmp_path):
    """The same commit, built from two fresh clones, is ONE runtime.

    Modelled on the measurement (2026-07-17), not on a guess: two `uv build`s of one tree are
    byte-identical, but a zip stamps each member with its source file's mtime, and a fresh clone
    writes new mtimes for identical code — so the two wheels differ AS FILES while their payloads
    match. A build id keyed on the file would mint a second build id, and a second 78MB interpreter
    copy, for code that has not changed.
    """
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    w1 = _wheel(a, stamp=(2026, 7, 17, 10, 0, 0))
    w2 = _wheel(b, stamp=(2026, 7, 18, 11, 30, 0))        # a fresh clone: same code, new mtimes
    assert runtime._file_sha256(w1) != runtime._file_sha256(w2), \
        "the fixture no longer models a re-clone; the assertion below would pass vacuously"
    assert runtime.build_id(w1, "cpython-3.11.15-darwin-arm64") == \
           runtime.build_id(w2, "cpython-3.11.15-darwin-arm64")


def test_build_id_changes_when_the_code_changes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    assert runtime.build_id(_wheel(a, payload=b"gen-a"), "i") != \
           runtime.build_id(_wheel(b, payload=b"gen-b"), "i")


def test_build_id_changes_when_the_interpreter_changes(tmp_path):
    """A patch bump is a different runtime. If it were not, generation N-1 and N could share a
    directory while running different interpreters — which is the mixing this prevents, one level
    down (nelix-cb0: a 3.13-only API green on 3.14, dead on the 3.11 daemon)."""
    w = _wheel(tmp_path)
    assert runtime.build_id(w, "cpython-3.11.15-darwin-arm64") != \
           runtime.build_id(w, "cpython-3.11.16-darwin-arm64")


def test_build_id_changes_when_the_locked_closure_changes(tmp_path):
    """wasmtime is a native wheel loaded into the daemon's address space; a runtime is not the same
    runtime with a different one pinned."""
    w = _wheel(tmp_path)
    lock_a, lock_b = tmp_path / "a.lock", tmp_path / "b.lock"
    lock_a.write_text("wasmtime==45.0.0 --hash=sha256:aa\n")
    lock_b.write_text("wasmtime==46.0.0 --hash=sha256:bb\n")
    assert runtime.build_id(w, "i", lock_a) != runtime.build_id(w, "i", lock_b)


def test_build_id_carries_the_readable_core_version(tmp_path):
    assert runtime.build_id(_wheel(tmp_path, version="2.5.0"), "i").startswith("2.5.0-")


# ---------------------------------------------------------------- commit semantics

def test_a_runtime_without_a_manifest_is_not_installed():
    """The manifest is written LAST, so its absence means the tree is a partial install — an
    interrupted copy, or one still being built. A venv cannot be staged elsewhere and renamed in
    (pyvenv.cfg's home is absolute), so 'the directory exists' can never be the commit signal."""
    _fake_runtime("0.1.0-partial", complete=False)
    assert paths.runtime_dir("0.1.0-partial").is_dir()
    assert runtime.is_installed("0.1.0-partial") is False
    assert "0.1.0-partial" not in runtime.installed()


def test_installed_lists_only_committed_runtimes():
    _fake_runtime("0.1.0-aaaaaaaaaaaa")
    _fake_runtime("0.1.0-bbbbbbbbbbbb")
    _fake_runtime("0.1.0-cccccccccccc", complete=False)
    assert runtime.installed() == ["0.1.0-aaaaaaaaaaaa", "0.1.0-bbbbbbbbbbbb"]


def test_python_for_refuses_a_partial_runtime():
    _fake_runtime("0.1.0-partial", complete=False)
    with pytest.raises(runtime.RuntimeInstallError):
        runtime.python_for("0.1.0-partial")


# ---------------------------------------------------------------- selection

def test_activate_moves_the_pointer_without_touching_the_runtime():
    """An upgrade's ONLY mutation. The old runtime must be byte-identical afterwards — that is what
    lets it be done while generation N-1 is live."""
    _fake_runtime("0.1.0-old")
    _fake_runtime("0.2.0-new")
    runtime.activate("0.1.0-old")
    before = {p: p.read_bytes() for p in paths.runtime_dir("0.1.0-old").rglob("*") if p.is_file()}
    runtime.activate("0.2.0-new")
    assert runtime.active() == "0.2.0-new"
    after = {p: p.read_bytes() for p in paths.runtime_dir("0.1.0-old").rglob("*") if p.is_file()}
    assert before == after, "activating a new generation modified the old one"


def test_activate_is_atomic_over_an_existing_pointer():
    """Re-pointing must never leave `current` missing, even for an instant: a daemon starting in
    that window would silently fall back to the checkout."""
    _fake_runtime("0.1.0-old")
    _fake_runtime("0.2.0-new")
    runtime.activate("0.1.0-old")
    runtime.activate("0.2.0-new")                      # over an existing symlink
    assert paths.runtime_current().is_symlink()
    assert os.readlink(paths.runtime_current()) == "0.2.0-new"


def test_activate_refuses_a_runtime_that_is_not_installed():
    with pytest.raises(runtime.RuntimeInstallError):
        runtime.activate("0.1.0-nonexistent")


def test_nelix_runtime_env_overrides_current(monkeypatch):
    _fake_runtime("0.1.0-current")
    _fake_runtime("0.2.0-pinned")
    runtime.activate("0.1.0-current")
    monkeypatch.setenv("NELIX_RUNTIME", "0.2.0-pinned")
    assert runtime.active() == "0.2.0-pinned"


def test_nelix_runtime_naming_a_missing_runtime_raises(monkeypatch):
    """NOT a reason to fall back to the checkout. Being asked for a specific generation and quietly
    running different code is the failure this whole slice exists to make impossible."""
    monkeypatch.setenv("NELIX_RUNTIME", "0.9.0-ghost")
    with pytest.raises(runtime.RuntimeInstallError):
        runtime.active()


def test_active_is_none_in_a_checkout_with_no_runtimes():
    assert runtime.active() is None
    assert runtime.active_python() is None


def test_a_current_pointing_at_a_partial_runtime_is_not_active():
    """A crash mid-install must not leave `current` naming a half-built generation."""
    _fake_runtime("0.1.0-partial", complete=False)
    paths.ensure_private_dir(paths.runtimes_root())
    paths.runtime_current().symlink_to("0.1.0-partial")
    assert runtime.active() is None
