"""Verify a release bundle, then build, activate and launch from it.

The plugin pins ONE digest — the manifest's — and everything else follows from it: the manifest names
every artifact by sha256, so checking the manifest and then the artifacts turns one pinned number
into a verified bundle. Verification happens BEFORE the first byte is written under $NELIX_HOME, so a
bad bundle leaves the machine exactly as it was.

When download lands: artifact STAGING (writing into a temp dir under $NELIX_HOME) MUST be inside the
distribution lock — unlike verification, staging IS a mutation and must not race with another
installer. Only the read-only checks stay outside.

Python 3.8-compatible, stdlib only: this runs on whatever python3 the machine has.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path

import launcher
import paths
import runtime


class BundleError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _sha256(path):
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@contextlib.contextmanager
def distribution_lock(wait_seconds=30):
    lock_path = paths.distribution_lock()
    paths.ensure_private_dir(lock_path.parent)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.time() + wait_seconds
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.time() >= deadline:
                    raise BundleError("install_in_progress",
                                      "another install is in progress in " + str(paths.nelix_root()))
                time.sleep(0.5)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid()}).encode())
        os.fsync(fd)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def verify_bundle(bundle_dir, manifest_sha256):
    """Check the manifest against the pinned digest, then every artifact against the manifest.
    Returns the classified paths. Raises BundleError with a stable code on any mismatch."""
    bundle_dir = Path(bundle_dir).resolve()
    manifest_path = bundle_dir / "release-manifest.json"
    if not manifest_path.exists():
        raise BundleError("manifest_missing", "no release-manifest.json in " + str(bundle_dir))

    actual = _sha256(manifest_path)
    if actual != manifest_sha256:
        raise BundleError("manifest_digest_mismatch",
                          "release-manifest.json is " + actual + ", pinned " + manifest_sha256)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    artifact_names = [a["name"] for a in manifest.get("artifacts", [])]
    if len(artifact_names) != len(set(artifact_names)):
        dups = sorted(n for n in set(artifact_names) if artifact_names.count(n) > 1)
        raise BundleError("duplicate_artifact_name",
                          "duplicate artifact names in manifest: " + ", ".join(dups))

    for art in manifest.get("artifacts", []):
        name = art["name"]
        pname = Path(name)
        if pname.name != name or '..' in name:
            raise BundleError("invalid_artifact_name",
                              "artifact name must be a simple filename, got " + repr(name))

        path = bundle_dir / name
        if path.is_symlink():
            raise BundleError("artifact_is_symlink",
                              "artifact must be a regular file, not a symlink: " + name)
        resolved = path.resolve()
        if bundle_dir not in [resolved] + list(resolved.parents):
            raise BundleError("artifact_path_escape",
                              "artifact resolves outside the bundle directory: " + name)
        if not resolved.is_file():
            raise BundleError("artifact_not_file",
                              "artifact must be a regular file: " + name)

        got = _sha256(path)
        if got != art["sha256"]:
            raise BundleError("artifact_digest_mismatch",
                              name + " is " + got + ", the manifest says " + art["sha256"])

    manifest_names = set(a["name"] for a in manifest.get("artifacts", []))
    for f in sorted(bundle_dir.iterdir()):
        if f.is_file() and f.name.endswith(".whl") and f.name not in manifest_names:
            raise BundleError("extra_wheel_in_bundle",
                              "wheel file not listed in manifest: " + f.name)

    wheels = [bundle_dir / a["name"] for a in manifest["artifacts"] if a["name"].endswith(".whl")]
    core = [w for w in wheels if w.name.startswith("nelix_core")]
    if len(core) != 1:
        raise BundleError("core_wheel_ambiguous",
                          "expected exactly one nelix_core wheel, found " + str([w.name for w in core]))

    lock_name = "requirements-runtime.lock"
    if lock_name not in manifest_names:
        raise BundleError("lock_missing",
                          "no requirements-runtime.lock in " + str(bundle_dir))

    lock_sha256_from_artifacts = next(a["sha256"] for a in manifest["artifacts"] if a["name"] == lock_name)
    if manifest.get("lock_sha256") != lock_sha256_from_artifacts:
        raise BundleError("lock_sha256_mismatch",
                          "manifest lock_sha256 " + repr(manifest.get("lock_sha256"))
                          + " disagrees with the lock's entry in artifacts " + lock_sha256_from_artifacts)

    lock = bundle_dir / lock_name

    return {"version": manifest.get("version"), "core_wheel": core[0], "lock": lock,
            "extra_wheels": sorted(w for w in wheels if w != core[0])}


def run_install(bundle_dir, manifest_sha256, home=None):
    """Verify -> build -> activate -> launcher. Returns an exit class; prints one JSON object."""
    from bootstrap.cli import EXIT_OK, EXIT_REJECTED, EXIT_UNAVAILABLE, emit, fail, require_prerequisites

    if home:
        os.environ["NELIX_HOME"] = str(home)

    try:
        checked = verify_bundle(bundle_dir, manifest_sha256)
    except BundleError as e:
        return fail(e.code, str(e), EXIT_REJECTED)

    missing = require_prerequisites()
    if missing:
        return fail(missing[0], missing[1], EXIT_UNAVAILABLE)

    try:
        with distribution_lock():
            try:
                build = runtime.install(checked["core_wheel"], lock=checked["lock"],
                                        extra_wheels=checked["extra_wheels"])
                runtime.activate(build)
                launcher_path = launcher.install(paths.nelix_root())
            except Exception as e:
                return fail("install_failed", str(e), EXIT_UNAVAILABLE)
    except BundleError as e:
        return fail(e.code, str(e), EXIT_UNAVAILABLE)

    emit({"build": build, "home": str(paths.nelix_root()), "launcher": str(launcher_path),
          "version": checked["version"]})
    return EXIT_OK


def run(args):
    return run_install(args.bundle, args.manifest_sha256, home=args.home)
