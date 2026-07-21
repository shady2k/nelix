"""Verification is the point: the plugin pins one digest, and everything else follows from it. A
bundle that fails any check must leave the machine EXACTLY as it was — no runtime dir, no launcher,
no half-written anything."""
import hashlib
import shutil

import pytest

import release
from bootstrap import install as bootstrap_install

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    d = tmp_path_factory.mktemp("bundle")
    manifest = release.build(d, version="0.1.0")
    return d, hashlib.sha256(manifest.read_bytes()).hexdigest()


def test_a_matching_bundle_verifies(bundle):
    d, digest = bundle

    out = bootstrap_install.verify_bundle(d, digest)

    assert out["version"] == "0.1.0"
    assert out["core_wheel"].name.startswith("nelix_core")
    assert len(out["extra_wheels"]) == 2


def test_a_wrong_pinned_digest_is_refused(bundle):
    d, _ = bundle

    with pytest.raises(bootstrap_install.BundleError) as ei:
        bootstrap_install.verify_bundle(d, "0" * 64)

    assert ei.value.code == "manifest_digest_mismatch"


def test_a_tampered_artifact_is_refused(bundle, tmp_path):
    d, digest = bundle
    copy = tmp_path / "tampered"
    shutil.copytree(d, copy)
    wheel = next(p for p in copy.glob("nelix_core*.whl"))
    wheel.write_bytes(wheel.read_bytes() + b"x")

    with pytest.raises(bootstrap_install.BundleError) as ei:
        bootstrap_install.verify_bundle(copy, digest)

    assert ei.value.code == "artifact_digest_mismatch"
    assert wheel.name in str(ei.value)


def test_a_missing_artifact_is_refused(bundle, tmp_path):
    d, digest = bundle
    copy = tmp_path / "missing"
    shutil.copytree(d, copy)
    next(p for p in copy.glob("nelix_store*.whl")).unlink()

    with pytest.raises(bootstrap_install.BundleError) as ei:
        bootstrap_install.verify_bundle(copy, digest)

    assert ei.value.code == "artifact_missing"


def test_verification_failure_writes_nothing_into_nelix_home(bundle, tmp_path, monkeypatch):
    """The strongest property here: a bad bundle leaves the machine as it was."""
    d, _ = bundle
    home = tmp_path / "home"
    monkeypatch.setenv("NELIX_HOME", str(home))

    rc = bootstrap_install.run_install(bundle_dir=d, manifest_sha256="0" * 64, home=str(home))

    assert rc == 5
    assert not home.exists() or list(home.iterdir()) == [], "a refused bundle must not touch $NELIX_HOME"


def test_a_real_install_produces_an_activated_runtime_and_a_launcher(bundle, tmp_path, monkeypatch):
    """End to end on a virgin home: build, activate, launcher — and the launcher then dispatches."""
    d, digest = bundle
    home = tmp_path / "home"
    monkeypatch.setenv("NELIX_HOME", str(home))

    rc = bootstrap_install.run_install(bundle_dir=d, manifest_sha256=digest, home=str(home))

    assert rc == 0
    assert (home / "bin" / "nelix").exists()
    current = home / "runtimes" / "current"
    assert current.is_symlink()
    assert (home / "runtimes" / current.readlink().name / "venv" / "bin" / "nelix").exists()
