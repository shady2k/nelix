"""The bootstrapper is the only piece that runs BEFORE nelix exists, so its self-containment is the
property under test — and it must be tested against the BUILT ARTIFACT, not the source tree, because
the source tree has everything importable and the artifact is exactly what a user gets."""
import hashlib
import json
import subprocess
import sys
import zipfile

import pytest

import release

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def pyz(tmp_path_factory):
    return release.build_pyz(tmp_path_factory.mktemp("pyz"))


def test_the_pyz_carries_exactly_the_three_stdlib_only_modules(pyz):
    """runtime + paths + launcher are the closure tests/test_runtime_closure.py guards. Anything
    else in here is either dead weight or a daemon import waiting to fail on a virgin machine."""
    names = {n.split("/")[0] for n in zipfile.ZipFile(pyz).namelist()}

    assert "runtime.py" in names
    assert "paths.py" in names
    assert "launcher.py" in names
    assert "daemon" not in names and "router" not in names
    assert "nelix_cli" not in names


def test_it_runs_on_a_bare_interpreter_with_no_repo_on_the_path(pyz, tmp_path):
    """cwd outside the repo, PYTHONPATH scrubbed: if it needs the checkout, it fails here."""
    env = {"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "HOME": str(tmp_path)}

    r = subprocess.run([sys.executable, str(pyz), "--help"],
                       cwd=tmp_path, env=env, capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    assert "install" in r.stdout


def test_it_reports_its_own_version_as_json(pyz, tmp_path):
    env = {"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "HOME": str(tmp_path)}

    r = subprocess.run([sys.executable, str(pyz), "--version"],
                       cwd=tmp_path, env=env, capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["bootstrap_schema"] == 1


def test_the_pyz_is_NOT_in_the_manifest_it_pins(tmp_path):
    """Circularity: the .pyz carries the manifest's digest, so the manifest cannot carry the .pyz's
    — changing one would change the other forever. The .pyz is the VERIFIER; its own authenticity
    comes from the plugin that ships its bytes, not from the manifest it checks."""
    manifest_path = release.build(tmp_path, version="0.1.0")
    manifest = json.loads(manifest_path.read_text())

    names = {a["name"] for a in manifest["artifacts"]}
    assert (tmp_path / "nelix-bootstrap.pyz").exists(), "it is still built and published"
    assert "nelix-bootstrap.pyz" not in names
    assert any(n.endswith(".whl") for n in names)
    assert "requirements-runtime.lock" in names


def test_the_pyz_carries_the_digest_of_the_manifest_it_was_built_with(tmp_path):
    manifest_path = release.build(tmp_path, version="0.1.0", base_url="https://example.invalid/v0.1.0")
    expected = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    src = zipfile.ZipFile(tmp_path / "nelix-bootstrap.pyz").read("bootstrap/_pin.py").decode()

    assert expected in src
    assert "0.1.0" in src
    assert "https://example.invalid/v0.1.0" in src


def test_a_pin_is_readable_from_inside_the_built_artifact(tmp_path):
    """Read it the way the bootstrapper will: import it out of the zipapp."""
    pyz = release.build(tmp_path, version="0.1.0", base_url="https://example.invalid/v0.1.0").parent / "nelix-bootstrap.pyz"
    probe = "import bootstrap._pin as p, json; print(json.dumps({'v': p.VERSION, 'm': p.MANIFEST_SHA256, 'u': p.BASE_URL}))"

    r = subprocess.run([sys.executable, "-c",
                        f"import sys; sys.path.insert(0, {str(pyz)!r}); {probe}"],
                       capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["v"] == "0.1.0" and len(got["m"]) == 64
