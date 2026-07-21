"""Fetching is where a supply chain gets attacked, so these tests serve a REAL bundle over a REAL
socket and then attack it: swap an artifact, swap the manifest, cut the network. The property is
always the same — nothing under $NELIX_HOME changes unless every digest matched."""
import hashlib
import http.server
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
