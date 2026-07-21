"""Two installs racing on one NELIX_HOME must not interleave: they write the same runtimes root and
the same launcher. The outer lock covers the mutation — build through launcher. Verification is a
read-only check and runs BEFORE the lock (failing fast on a bad bundle without waiting)."""
import threading

import pytest

import paths
from bootstrap import install as bootstrap_install


def test_the_lock_lives_under_nelix_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))

    assert paths.distribution_lock().parent.parent == paths.nelix_root()


def test_a_second_installer_is_refused_while_the_first_holds_it(tmp_path, monkeypatch):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    held = threading.Event()
    release_it = threading.Event()

    def _hold():
        with bootstrap_install.distribution_lock(wait_seconds=0):
            held.set()
            release_it.wait(timeout=10)

    t = threading.Thread(target=_hold)
    t.start()
    try:
        assert held.wait(timeout=5)
        with pytest.raises(bootstrap_install.BundleError) as ei:
            with bootstrap_install.distribution_lock(wait_seconds=0):
                pass
        assert ei.value.code == "install_in_progress"
    finally:
        release_it.set()
        t.join(timeout=5)


def test_the_lock_is_released_when_the_body_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))

    with pytest.raises(RuntimeError):
        with bootstrap_install.distribution_lock(wait_seconds=0):
            raise RuntimeError("boom")

    with bootstrap_install.distribution_lock(wait_seconds=0):
        pass                       # acquiring again proves the first release happened
