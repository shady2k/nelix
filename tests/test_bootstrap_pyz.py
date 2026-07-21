"""The bootstrapper is the only piece that runs BEFORE nelix exists, so its self-containment is the
property under test — and it must be tested against the BUILT ARTIFACT, not the source tree, because
the source tree has everything importable and the artifact is exactly what a user gets."""
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


def test_a_release_build_emits_the_pyz_beside_the_wheels(tmp_path):
    manifest_path = release.build(tmp_path, version="0.1.0")
    manifest = json.loads(manifest_path.read_text())

    assert (tmp_path / "nelix-bootstrap.pyz").exists()
    names = {a["name"] for a in manifest["artifacts"]}
    assert "nelix-bootstrap.pyz" in names, (
        "the pyz must be IN the manifest: a plugin pins its digest, so a release that ships it "
        "unnamed ships it untrusted")
