"""The store's active generation is the truth; runtimes/current is a cache of it. A crash between
the flip and the symlink write leaves them disagreeing, and router/app.py reads the SYMLINK to
decide which build new daemons are spawned from — so an unrepaired disagreement silently runs the
wrong code. Reconciliation happens at startup, before anything reads it.
"""
import json
from types import SimpleNamespace

import pytest

import paths
import runtime
from nelix_contracts.ids import new_generation_id
from nelix_store.store import Store
from router.reconcile_current import reconcile

_ACTIVE_BUILD = "0.1.0-bbbbbbbbbbbb"
_OLDER_BUILD = "0.1.0-aaaaaaaaaaaa"


def _fake_runtime(build, *, complete=True):
    py = paths.runtime_python(build)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\nexec /usr/bin/true\n")
    py.chmod(0o755)
    if complete:
        paths.runtime_manifest(build).write_text(json.dumps({"build_id": build}))
    return build


@pytest.fixture
def recon_env():
    store = Store(paths.nelix_root(), clock=lambda: 1000.0)

    _fake_runtime(_OLDER_BUILD)
    _fake_runtime(_ACTIVE_BUILD)
    runtime.activate(_ACTIVE_BUILD)

    gid = new_generation_id()
    epoch = new_generation_id()
    store.create_generation(
        gid, build_id=_ACTIVE_BUILD, lifecycle_state="active",
        capability_snapshot=None, created_at=1000.0,
    )
    store.insert_epoch(epoch, gid, incarnation_meta=None, created_at=1000.0)
    store.cas_epoch_serving(gid, epoch, expected_current_epoch=None)

    def _uninstall_active_build():
        paths.runtime_manifest(_ACTIVE_BUILD).unlink(missing_ok=True)

    def _clear_active_generation():
        for g in store.list_generations():
            if g.lifecycle_state == "active":
                store.set_generation_lifecycle_state(g.generation_id, "draining")
                break

    env = SimpleNamespace(
        store=store,
        active_build=_ACTIVE_BUILD,
        older_build=_OLDER_BUILD,
        uninstall_active_build=_uninstall_active_build,
        clear_active_generation=_clear_active_generation,
    )
    yield env
    store.close()


def test_agreement_is_a_no_op(recon_env):
    """The overwhelmingly common case must touch nothing."""
    store, build = recon_env.store, recon_env.active_build

    out = reconcile(store)

    assert out["action"] == "none"
    assert out["authoritative"] == build
    assert runtime.active() == build


def test_a_stale_pointer_is_repaired_to_the_authoritative_build(recon_env):
    """The crash case: the store flipped, the symlink did not."""
    store, build, older = recon_env.store, recon_env.active_build, recon_env.older_build
    runtime.activate(older)
    assert runtime.active() == older

    out = reconcile(store)

    assert out["action"] == "repaired"
    assert out["current"] == older, "the report must name what it found"
    assert out["authoritative"] == build
    assert runtime.active() == build


def test_an_absent_pointer_is_repaired_too(recon_env):
    store, build = recon_env.store, recon_env.active_build
    paths.runtime_current().unlink()

    out = reconcile(store)

    assert out["action"] == "repaired"
    assert runtime.active() == build


def test_an_uninstalled_authoritative_build_is_reported_not_forced(recon_env):
    """If the store names a build whose runtime is gone, pointing `current` at it would create a
    dangling pointer — worse than a stale one. Report it and leave the pointer alone."""
    store, older = recon_env.store, recon_env.older_build
    runtime.activate(older)
    recon_env.uninstall_active_build()

    out = reconcile(store)

    assert out["action"] == "unrepairable"
    assert runtime.active() == older, "a dangling pointer is worse than a stale one"


def test_no_active_generation_is_a_no_op(recon_env):
    """A fresh install with no generation yet must not be 'repaired' to anything."""
    store = recon_env.store
    recon_env.clear_active_generation()

    out = reconcile(store)

    assert out["action"] == "none"
    assert out["authoritative"] is None


def test_it_never_raises_when_the_store_is_unreadable(recon_env, monkeypatch):
    """Reconciliation runs on the router's startup path. A router that refuses to start because a
    CACHE could not be checked is a worse outcome than a stale cache."""
    def _boom():
        raise RuntimeError("store is gone")

    monkeypatch.setattr(recon_env.store, "list_generations", _boom)

    out = reconcile(recon_env.store)

    assert out["action"] == "none"


def test_the_build_is_pinned_only_after_the_cache_is_reconciled(recon_env, monkeypatch):
    """Ordering IS the property: reconciling after the read would pin the stale build for the life
    of this router."""
    import router.app as app
    import router.reconcile_current as rc

    order = []
    real = rc.reconcile

    def _spy(store, **kw):
        order.append("reconcile")
        return real(store, **kw)

    monkeypatch.setattr(rc, "reconcile", _spy)
    monkeypatch.setattr(runtime, "active", lambda: order.append("read_current") or "b-x")

    app._pin_active_build(recon_env.store)

    assert order[0] == "reconcile", "reconcile must be called first"
    assert order[-1] == "read_current", "the final active() read must happen after reconcile"


def test_pinning_survives_a_checkout_without_an_installed_runtime(recon_env, monkeypatch):
    """A dev checkout has no runtime module state to speak of; the router must still start."""
    import router.app as app

    monkeypatch.setattr(runtime, "active", lambda: None)

    assert app._pin_active_build(recon_env.store) is None
