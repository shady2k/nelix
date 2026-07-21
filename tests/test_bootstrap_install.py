"""Verification is the point: the plugin pins one digest, and everything else follows from it. A
bundle that fails any check must leave the machine EXACTLY as it was — no runtime dir, no launcher,
no half-written anything."""
import contextlib
import hashlib
import io as _io
import json as _json
import shutil
import sys as _sys
import threading

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

    assert ei.value.code == "artifact_not_file"


def test_a_tampered_lock_is_refused(bundle, tmp_path):
    """The lock is an ordinary artifact now — tampering with its content must fail the digest check."""
    d, digest = bundle
    copy = tmp_path / "tampered_lock"
    shutil.copytree(d, copy)
    lock = copy / "requirements-runtime.lock"
    lock.write_bytes(lock.read_bytes() + b"// tampered")

    with pytest.raises(bootstrap_install.BundleError) as ei:
        bootstrap_install.verify_bundle(copy, digest)

    assert ei.value.code == "artifact_digest_mismatch"
    assert "requirements-runtime.lock" in str(ei.value)


def test_an_artifact_name_escaping_the_directory_is_refused(bundle, tmp_path):
    d, digest = bundle
    copy = tmp_path / "escape_name"
    shutil.copytree(d, copy)
    manifest_path = copy / "release-manifest.json"
    manifest = _json.loads(manifest_path.read_text())
    manifest["artifacts"].append({"name": "../evil.whl", "sha256": "0" * 64, "size": 0})
    new_manifest_bytes = _json.dumps(manifest).encode()
    manifest_path.write_bytes(new_manifest_bytes)
    new_digest = hashlib.sha256(new_manifest_bytes).hexdigest()

    with pytest.raises(bootstrap_install.BundleError) as ei:
        bootstrap_install.verify_bundle(copy, new_digest)

    assert ei.value.code == "invalid_artifact_name"


def test_an_artifact_symlink_pointing_outside_is_refused(bundle, tmp_path):
    d, digest = bundle
    copy = tmp_path / "symlink_out"
    shutil.copytree(d, copy)
    manifest_path = copy / "release-manifest.json"
    manifest = _json.loads(manifest_path.read_text())

    outside = tmp_path / "outside.whl"
    outside.write_bytes(b"evil")
    outside_hash = hashlib.sha256(outside.read_bytes()).hexdigest()
    (copy / "evil_link.whl").symlink_to(outside)

    manifest["artifacts"].append({"name": "evil_link.whl", "sha256": outside_hash, "size": len(b"evil")})
    manifest_path.write_text(_json.dumps(manifest))

    with pytest.raises(bootstrap_install.BundleError) as ei:
        bootstrap_install.verify_bundle(copy, hashlib.sha256(manifest_path.read_bytes()).hexdigest())

    assert ei.value.code in ("artifact_is_symlink", "artifact_path_escape")


def test_a_duplicate_artifact_name_is_refused(bundle, tmp_path):
    d, digest = bundle
    copy = tmp_path / "duplicate_name"
    shutil.copytree(d, copy)
    manifest_path = copy / "release-manifest.json"
    manifest = _json.loads(manifest_path.read_text())
    manifest["artifacts"].append(manifest["artifacts"][0].copy())
    new_manifest_bytes = _json.dumps(manifest).encode()
    manifest_path.write_bytes(new_manifest_bytes)
    new_digest = hashlib.sha256(new_manifest_bytes).hexdigest()

    with pytest.raises(bootstrap_install.BundleError) as ei:
        bootstrap_install.verify_bundle(copy, new_digest)

    assert ei.value.code == "duplicate_artifact_name"


def test_an_extra_wheel_not_named_in_the_manifest_is_refused(bundle, tmp_path):
    d, digest = bundle
    copy = tmp_path / "extra_wheel"
    shutil.copytree(d, copy)
    (copy / "intruder-1.0.0-py3-none-any.whl").write_bytes(b"i am not in the manifest")

    with pytest.raises(bootstrap_install.BundleError) as ei:
        bootstrap_install.verify_bundle(copy, digest)

    assert ei.value.code == "extra_wheel_in_bundle"


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


def test_a_held_lock_gives_a_clean_json_not_a_traceback(bundle, tmp_path, monkeypatch):
    """run_install must return a clean JSON envelope even when the lock is held — never a traceback."""
    d, digest = bundle
    home = tmp_path / "home"
    monkeypatch.setenv("NELIX_HOME", str(home))

    held = threading.Event()
    release_it = threading.Event()

    def _hold():
        with bootstrap_install.distribution_lock(wait_seconds=30):
            held.set()
            release_it.wait(timeout=10)

    t = threading.Thread(target=_hold)
    t.start()
    held.wait(timeout=5)

    saved_out = _sys.stdout
    saved_err = _sys.stderr
    _sys.stdout = _io.StringIO()
    _sys.stderr = _io.StringIO()

    try:
        _orig = bootstrap_install.distribution_lock

        @contextlib.contextmanager
        def _instant_lock():
            with _orig(wait_seconds=0):
                yield

        bootstrap_install.distribution_lock = _instant_lock

        rc = bootstrap_install.run_install(bundle_dir=d, manifest_sha256=digest, home=str(home))
        stdout = _sys.stdout.getvalue()
        stderr = _sys.stderr.getvalue()
    finally:
        bootstrap_install.distribution_lock = _orig
        _sys.stdout = saved_out
        _sys.stderr = saved_err
        release_it.set()
        t.join(timeout=5)

    assert rc == 3, f"expected EXIT_UNAVAILABLE (3), got {rc}; stderr: {stderr}"
    assert "Traceback" not in stdout, "stdout must not contain a traceback"
    assert "Traceback" not in stderr, "stderr must not contain a traceback"
    try:
        envelope = _json.loads(stdout.strip())
    except _json.JSONDecodeError:
        pytest.fail(f"stdout must be a single JSON object, got: {stdout!r}")
    assert envelope.get("ok") is False
    assert envelope["error"]["code"] == "install_in_progress"
