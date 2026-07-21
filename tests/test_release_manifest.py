"""The release manifest is what an upgrade verifies BEFORE touching a runtime dir: it names every
artifact of one release by sha256, so a substituted or truncated byte is caught by the installer
rather than by a mysterious failure hours later. These tests pin its shape and its honesty."""
import hashlib
import json

import pytest

import release


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_manifest_names_every_artifact_with_its_digest_and_size(tmp_path):
    a = tmp_path / "nelix_core-0.1.0-py3-none-any.whl"
    a.write_bytes(b"core bytes")
    b = tmp_path / "nelix_store-0.1.0-py3-none-any.whl"
    b.write_bytes(b"store bytes")

    m = release.manifest_for([a, b], version="0.1.0", lock_sha256="0" * 64)

    assert m["schema_version"] == release.MANIFEST_SCHEMA
    assert m["version"] == "0.1.0"
    assert {art["name"] for art in m["artifacts"]} == {a.name, b.name}
    by_name = {art["name"]: art for art in m["artifacts"]}
    assert by_name[a.name]["sha256"] == _sha256(a)
    assert by_name[a.name]["size"] == len(b"core bytes")


def test_manifest_declares_the_planes_an_adapter_negotiates_against(tmp_path):
    m = release.manifest_for([], version="0.1.0", lock_sha256="0" * 64)

    assert m["cli_api"] == 1
    assert m["requires_python"] == "==3.11.*"
    assert m["lock_sha256"] == "0" * 64


def test_a_changed_artifact_changes_the_manifest(tmp_path):
    a = tmp_path / "x.whl"
    a.write_bytes(b"one")
    first = release.manifest_for([a], version="0.1.0", lock_sha256="0" * 64)
    a.write_bytes(b"two")
    second = release.manifest_for([a], version="0.1.0", lock_sha256="0" * 64)

    assert first != second, "a manifest that cannot see a changed artifact verifies nothing"


@pytest.mark.slow
def test_build_emits_three_wheels_the_lock_and_a_truthful_manifest(tmp_path):
    manifest_path = release.build(tmp_path, version="0.1.0")
    m = json.loads(manifest_path.read_text())

    names = {art["name"] for art in m["artifacts"]}
    assert sum(1 for n in names if n.startswith("nelix_core")) == 1
    assert sum(1 for n in names if n.startswith("nelix_store")) == 1
    assert sum(1 for n in names if n.startswith("nelix_contracts")) == 1

    for art in m["artifacts"]:
        on_disk = tmp_path / art["name"]
        assert on_disk.exists(), f"manifest names {art['name']} but it is not in the release dir"
        assert _sha256(on_disk) == art["sha256"]
        assert on_disk.stat().st_size == art["size"]

    assert (tmp_path / "requirements-runtime.lock").exists()
    assert m["lock_sha256"] == _sha256(tmp_path / "requirements-runtime.lock")
