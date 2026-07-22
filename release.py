"""Build one release: the core wheel, the two first-party wheels, the runtime lock, and a manifest
that names each by sha256.

The manifest is the unit of trust in the distribution design: a plugin pins its digest, the
installer verifies every artifact against it BEFORE touching a runtime directory, and an upgrade is
validated against it while the release is still just bytes. So this module's whole job is to make
the manifest's claims true — it hashes what it actually wrote, never what it meant to write.

stdlib only, and no import of daemon/ or router/: the same manifest has to be readable by a
bootstrapper that carries neither.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipapp
from pathlib import Path

MANIFEST_SCHEMA = 1
CLI_API = 1
REQUIRES_PYTHON = "==3.11.*"

REPO = Path(__file__).resolve().parent
LOCK = REPO / "requirements-runtime.lock"
PROJECTS = (REPO, REPO / "packages" / "nelix_store", REPO / "packages" / "nelix_contracts")

_PYZ_MODULES = ("runtime.py", "paths.py", "launcher.py")


_ZIP_EPOCH = 315532800  # 1980-01-01 00:00:00 UTC — the ZIP format minimum timestamp


def build_pyz(dist_dir) -> Path:
    """Stage the bootstrapper and its three carried modules, and zip them into nelix-bootstrap.pyz.
    File timestamps are normalised to a fixed epoch so that builds from identical source code
    produce bit-for-bit identical .pyz archives."""
    dist_dir = Path(dist_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)
    stage = dist_dir / "_pyz_stage"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "bootstrap").mkdir(parents=True)

    for name in _PYZ_MODULES:
        shutil.copy2(REPO / name, stage / name)
    for name in ("__init__.py", "__main__.py", "cli.py", "install.py"):
        shutil.copy2(REPO / "bootstrap" / name, stage / "bootstrap" / name)
    shutil.copy2(REPO / "bootstrap" / "__main__.py", stage / "__main__.py")

    # Normalise every staged file's mtime to a fixed epoch so the zip archives are
    # deterministic — two builds from the same source must produce the same sha256.
    for f in sorted(stage.rglob("*")):
        if f.is_file():
            os.utime(f, (_ZIP_EPOCH, _ZIP_EPOCH))

    out = dist_dir / "nelix-bootstrap.pyz"
    zipapp.create_archive(stage, target=out, interpreter="/usr/bin/env python3")
    shutil.rmtree(stage, ignore_errors=True)
    return out


def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_for(artifacts, *, version: str, lock_sha256: str) -> dict:
    """The manifest as data. Artifacts are hashed FROM DISK here, so a caller cannot hand in a
    digest that does not match the bytes it shipped."""
    return {
        "schema_version": MANIFEST_SCHEMA,
        "version": version,
        "requires_python": REQUIRES_PYTHON,
        "cli_api": CLI_API,
        "lock_sha256": lock_sha256,
        "artifacts": sorted(
            ({"name": Path(a).name, "sha256": file_sha256(a), "size": Path(a).stat().st_size}
             for a in artifacts),
            key=lambda art: art["name"]),
    }


def build(dist_dir, *, version: str) -> Path:
    """Build every wheel into `dist_dir`, copy the lock beside them, and write release-manifest.json.
    Returns the manifest path.

    Order: wheels + lock -> manifest -> .pyz. The manifest covers what gets INSTALLED
    (wheels + the lock) — deliberately NOT the .pyz, which is built AFTERWARDS and does
    not carry a pin; the pin is the subscriber's responsibility to generate."""
    dist_dir = Path(dist_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)
    for project in PROJECTS:
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(dist_dir), str(project)],
                       check=True)
    shutil.copy2(LOCK, dist_dir / LOCK.name)

    artifacts = sorted(list(dist_dir.glob("*.whl")) + [dist_dir / LOCK.name])
    manifest = manifest_for(artifacts, version=version,
                            lock_sha256=file_sha256(dist_dir / LOCK.name))
    out = dist_dir / "release-manifest.json"
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    build_pyz(dist_dir)
    return out


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="release", description="build a nelix release bundle")
    ap.add_argument("--dist", default=str(REPO / "dist"))
    ap.add_argument("--version", required=True)
    args = ap.parse_args(argv)
    print(build(args.dist, version=args.version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
