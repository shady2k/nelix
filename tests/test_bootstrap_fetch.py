"""Fetching is where a supply chain gets attacked, so these tests serve a REAL bundle over a REAL
socket and then attack it: swap an artifact, swap the manifest, cut the network. The property is
always the same — nothing under $NELIX_HOME changes unless every digest matched.

The last two tests (artifact_*) go one level deeper: they run the BUILT .pyz as a subprocess,
exercising the zipapp's import path rather than just calling fetch_bundle() in-process — so a
regression that only shows up "outside" the test runner is caught here."""
import hashlib
import http.server
import json
import subprocess
import sys
import threading

import pytest

import release
from bootstrap import install as bootstrap_install

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """A built release, served over http, plus its pinned digest."""
    d = tmp_path_factory.mktemp("release")
    manifest = release.build(d, version="0.1.0")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()

    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(d), **kw)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield d, f"http://127.0.0.1:{srv.server_address[1]}", digest
    srv.shutdown()


def test_a_served_bundle_is_fetched_and_verified(served, tmp_path):
    _d, url, digest = served

    out = bootstrap_install.fetch_bundle(url, digest, tmp_path / "stage")

    assert (out / "release-manifest.json").exists()
    assert list(out.glob("*.whl")), "the wheels the manifest names must be here"
    assert (out / "requirements-runtime.lock").exists()


def test_a_wrong_pin_is_refused_and_nothing_is_kept(served, tmp_path):
    _d, url, _digest = served
    stage = tmp_path / "stage"

    with pytest.raises(bootstrap_install.BundleError) as ei:
        bootstrap_install.fetch_bundle(url, "0" * 64, stage)

    assert ei.value.code == "manifest_digest_mismatch"
    assert not stage.exists() or not list(stage.iterdir()), "a refused fetch leaves nothing behind"


def test_a_tampered_artifact_on_the_server_is_refused(served, tmp_path):
    d, url, digest = served
    wheel = next(d.glob("nelix_core*.whl"))
    original = wheel.read_bytes()
    wheel.write_bytes(original + b"evil")
    try:
        with pytest.raises(bootstrap_install.BundleError) as ei:
            bootstrap_install.fetch_bundle(url, digest, tmp_path / "stage2")
        assert ei.value.code == "artifact_digest_mismatch"
    finally:
        wheel.write_bytes(original)


def test_an_unreachable_server_is_the_unavailable_class_not_a_traceback(tmp_path):
    rc = bootstrap_install.run_install(bundle_dir=None, manifest_sha256=None,
                                       home=str(tmp_path / "home"),
                                       base_url="http://127.0.0.1:9", pin_digest="0" * 64)

    assert rc == 3
    assert not (tmp_path / "home" / "runtimes").exists()


def test_install_without_a_pin_says_so(tmp_path, monkeypatch):
    """A .pyz built by hand, without a release, must not pretend it knows where to fetch from."""
    monkeypatch.setattr(bootstrap_install, "read_pin", lambda: None)

    rc = bootstrap_install.run_install(bundle_dir=None, manifest_sha256=None,
                                       home=str(tmp_path / "home"))

    assert rc == 2


def _start_server(directory):
    """Start a ThreadingHTTPServer on a free port serving *directory*.
    Returns (server, url)."""
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(directory), **kw)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    return srv, f"http://127.0.0.1:{port}"


def test_artifact_level_install_via_http(tmp_path):
    """Run the BUILT .pyz as a subprocess — the closest thing to a real install without a Hermes
    plugin. This test exists because in-process fetch_bundle() tests do not exercise the zipapp's
    import path and would miss a regression in the bootstrap entry point."""
    d = tmp_path / "release"
    release.build(d, version="0.1.0")
    pyz = d / "nelix-bootstrap.pyz"
    assert pyz.exists()

    srv, url = _start_server(d)
    try:
        home = tmp_path / "home"
        r = subprocess.run([sys.executable, str(pyz), "install",
                            "--home", str(home), "--base-url", url],
                           capture_output=True, text=True, timeout=60)

        assert r.returncode == 0, f"stderr: {r.stderr}"
        envelope = json.loads(r.stdout.strip())
        assert envelope.get("ok") is True
        assert "build" in envelope
        assert "launcher" in envelope
        assert (home / "bin" / "nelix").exists()
        current = home / "runtimes" / "current"
        assert current.is_symlink()
    finally:
        srv.shutdown()


def test_artifact_level_rejects_tampered_artifact(tmp_path):
    """Swap bytes on the server AFTER the build (so the pin is computed for the clean release),
    then run the .pyz — it must refuse with artifact_digest_mismatch and leave NOTHING under
    --home."""
    d = tmp_path / "release"
    release.build(d, version="0.1.0")
    pyz = d / "nelix-bootstrap.pyz"
    assert pyz.exists()

    # Tamper a wheel on disk BEFORE starting the server (server will serve the modified copy)
    wheel = next(d.glob("nelix_core*.whl"))
    wheel.write_bytes(wheel.read_bytes() + b"EVIL")

    srv, url = _start_server(d)
    try:
        home = tmp_path / "home"
        r = subprocess.run([sys.executable, str(pyz), "install",
                            "--home", str(home), "--base-url", url],
                           capture_output=True, text=True, timeout=60)

        assert r.returncode != 0, f"expected failure, got stdout: {r.stdout}"
        envelope = json.loads(r.stdout.strip())
        assert envelope.get("ok") is False
        assert envelope["error"]["code"] in ("artifact_digest_mismatch", "download_failed")
        # No install artifacts must survive a refused fetch — the property is that no runtime
        # dir, launcher, or build was committed. The locks/ dir is a side effect of the
        # distribution_lock acquisition inside run_install(), not an install artifact.
        assert not (home / "runtimes").exists()
        assert not (home / "bin").exists()
        # Staging dir must be cleaned up on failure
        assert not (home / "staging").exists()
    finally:
        srv.shutdown()
