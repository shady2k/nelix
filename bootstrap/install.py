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
import re
import shutil
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import launcher
import paths
import runtime

# The subscriber's pin file identifies WHAT release to fetch (version + manifest digest).
# WHERE to fetch it from is a stable identity compiled into the bootstrap — a pin file
# must never be able to redirect the installer at an arbitrary host.
BASE_URL = "https://github.com/shady2k/nelix/releases/download"
PIN_SCHEMA_VERSION = 1


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


def _hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def read_pin_file(path):
    """Read and strictly validate a pin JSON file.

    A valid pin file has exactly this shape:
        {"schema_version": 1, "version": "0.2.0", "manifest_sha256": "<64 lowercase hex>"}

    There is NO base_url in the file — the repository identity is a stable constant (BASE_URL)
    compiled into the bootstrap. Returns (version, manifest_sha256) on success.
    Raises BundleError with a stable code on any validation failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise BundleError("pin_file_missing", "pin file not found: " + str(path)) from e
    except json.JSONDecodeError as e:
        raise BundleError("pin_file_invalid", "pin file is not valid JSON: " + str(e)) from e

    allowed_keys = {"schema_version", "version", "manifest_sha256"}
    extra = set(data.keys()) - allowed_keys
    if extra:
        raise BundleError("pin_file_invalid",
                          "unexpected keys in pin file: " + ", ".join(sorted(extra)))

    if "schema_version" not in data:
        raise BundleError("pin_file_invalid", "pin file missing 'schema_version'")
    if data["schema_version"] != PIN_SCHEMA_VERSION:
        raise BundleError("pin_file_invalid",
                          "unknown schema_version " + repr(data["schema_version"])
                          + ", expected " + str(PIN_SCHEMA_VERSION))

    if "version" not in data:
        raise BundleError("pin_file_invalid", "pin file missing 'version'")
    version = data["version"]
    if not isinstance(version, str) or not re.match(r"^\d+\.\d+\.\d+$", version):
        raise BundleError("pin_file_invalid",
                          "version must be X.Y.Z, got " + repr(version))

    if "manifest_sha256" not in data:
        raise BundleError("pin_file_invalid", "pin file missing 'manifest_sha256'")
    digest = data["manifest_sha256"]
    if not isinstance(digest, str) or not re.match(r"^[0-9a-f]{64}$", digest):
        raise BundleError("pin_file_invalid",
                          "manifest_sha256 must be 64 lowercase hex chars, got " + repr(digest))

    return version, digest


_DOWNLOAD_TIMEOUT = 30


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


def fetch_bundle(base_url, manifest_sha256, dest):
    """Download the manifest from *base_url*, verify its digest against *manifest_sha256*,
    then download every artifact the manifest names, verifying each against its claimed sha256.
    *dest* is created fresh; on any error it is removed before the exception propagates.
    Returns *dest* (the bundle directory with all files)."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    try:
        actual_digest = _download_to(dest, base_url, "release-manifest.json")
        if actual_digest != manifest_sha256:
            raise BundleError("manifest_digest_mismatch",
                              "release-manifest.json is " + actual_digest
                              + ", pinned " + manifest_sha256)

        manifest = json.loads((dest / "release-manifest.json").read_text(encoding="utf-8"))
        for art in manifest.get("artifacts", []):
            name = art["name"]
            pname = Path(name)
            if pname.name != name or ".." in name:
                raise BundleError("invalid_artifact_name",
                                  "artifact name must be a simple filename, got " + repr(name))
            art_digest = _download_to(dest, base_url, name)
            if art_digest != art["sha256"]:
                raise BundleError("artifact_digest_mismatch",
                                  name + " is " + art_digest + ", the manifest says " + art["sha256"])
        return dest
    except BundleError:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _download_to(dest_dir, base_url, name):
    """Download ``base_url/name`` into ``dest_dir/name``. Returns the sha256 of the downloaded
    bytes. Raises BundleError("download_failed", ...) on any network or I/O error."""
    url = base_url.rstrip("/") + "/" + name
    try:
        resp = urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT)
        data = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError) as e:
        raise BundleError("download_failed", "cannot fetch " + url + ": " + str(e)) from e
    (dest_dir / name).write_bytes(data)
    return _hash_bytes(data)


def run_install(bundle_dir, manifest_sha256, home=None, base_url=None, pin_digest=None):
    """Verify -> build -> activate -> launcher. Returns an exit class; prints one JSON object.

    When *bundle_dir* is None, fetches from *base_url* using *pin_digest* as the trusted
    manifest digest. Fetching writes under NELIX_HOME, so it runs INSIDE distribution_lock —
    unlike the offline path, where verification is a read-only check.
    """
    from bootstrap.cli import EXIT_OK, EXIT_REJECTED, EXIT_UNAVAILABLE, EXIT_USAGE, emit, fail, require_prerequisites

    if home:
        os.environ["NELIX_HOME"] = str(home)

    if bundle_dir is None:
        if pin_digest is None or base_url is None:
            return fail("no_pin",
                        "this .pyz was hand-built without a release pin; "
                        "provide --bundle and --manifest-sha256",
                        EXIT_USAGE)
        try:
            with distribution_lock():
                staging = paths.nelix_root() / "staging"
                bundle_dir = fetch_bundle(base_url, pin_digest, staging)
        except BundleError as e:
            return fail(e.code, str(e), EXIT_UNAVAILABLE)
        manifest_sha256 = pin_digest  # the pin IS the pinned manifest digest

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
    """Dispatch from the CLI.

    Three modes:
    1. --bundle + --manifest-sha256  → offline install from a local release bundle.
    2. --pin-file <path>             → fetch the pinned release from BASE_URL/v<version>.
    3. neither                       → usage error.
    """
    if args.bundle is not None:
        return run_install(args.bundle, args.manifest_sha256, home=args.home)

    if args.pin_file is not None:
        try:
            version, digest = read_pin_file(args.pin_file)
        except BundleError as e:
            from bootstrap.cli import EXIT_USAGE, fail
            return fail(e.code, str(e), EXIT_USAGE)
        # The base URL for fetching is a stable constant; --base-url overrides the source
        # but NEVER the digest — the pin file is the sole source of trust for what to install.
        base_url = args.base_url or (BASE_URL + "/v" + version)
        return run_install(bundle_dir=None, manifest_sha256=digest,
                           home=args.home, base_url=base_url, pin_digest=digest)

    from bootstrap.cli import EXIT_USAGE, fail
    return fail("no_pin",
                "a pin file is required; provide --pin-file <path> or --bundle + --manifest-sha256",
                EXIT_USAGE)
