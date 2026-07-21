"""`runtimes/current` is the pointer NEW daemons are built from — router/app.py reads it at startup
to pin the registry's build_id. If a promotion moves the active generation but leaves the pointer
behind, the next router start spawns daemons from the PREVIOUS build while the store insists the new
one is active. So a successful promotion must move it, and a failed one must not.
"""
import json
from types import SimpleNamespace

import pytest

import paths
import runtime
from nelix_store.store import Store
from router.operator import OperatorRoutes
from router.registry import GenerationRegistry
from tests._router_fakes import Backend, Supervisor

_EPOCH = "r-" + "0" * 32

_OLDER_BUILD = "0.1.0-aaaaaaaaaaaa"
_NEWER_BUILD = "0.1.0-bbbbbbbbbbbb"


def _fake_runtime(build, *, complete=True):
    py = paths.runtime_python(build)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\nexec /usr/bin/true\n")
    py.chmod(0o755)
    if complete:
        paths.runtime_manifest(build).write_text(json.dumps({"build_id": build}))
    return build


class _FakeGenSup:
    _transport = None

    def __init__(self, gid, build_id):
        self.gid = gid
        self.build_id = build_id

    def ensure_generation_dirs(self):
        pass

    def ensure_running(self, generation_epoch):
        return {"pid": 9999, "start_fingerprint": "test-fp"}, self._transport

    def _check_health_strict(self, transport, expected_epoch, expected_gid, expected_build):
        return True

    def _live_lock_holder(self):
        return {"pid": 9999, "start_fingerprint": "test-fp"}

    def reap_holder(self, expected_incarnation):
        pass


@pytest.fixture
def operator_env(monkeypatch):
    store = Store(paths.nelix_root(), clock=lambda: 1000.0)
    backend = Backend(build_id=_NEWER_BUILD)
    registry = GenerationRegistry(
        supervisor=Supervisor(backend.transport),
        build_id="b-registry",
        health_probe=lambda t: "b-registry",
    )
    operator = OperatorRoutes(registry, _EPOCH, store=store)

    _FakeGenSup._transport = backend.transport
    monkeypatch.setattr("generation_supervisor.GenerationSupervisor", _FakeGenSup)

    _fake_runtime(_OLDER_BUILD)
    paths.ensure_private_dir(paths.runtimes_root())
    runtime.activate(_OLDER_BUILD)
    assert runtime.active() == _OLDER_BUILD

    _fake_runtime(_NEWER_BUILD)
    assert runtime.is_installed(_NEWER_BUILD)

    yield SimpleNamespace(
        operator=operator,
        build=_NEWER_BUILD,
        older_build=_OLDER_BUILD,
        store=store,
        backend=backend,
        registry=registry,
    )
    backend.close()
    store.close()


def test_a_successful_activation_points_current_at_the_activated_build(operator_env):
    """The forward direction: store flip and symlink agree once activate returns."""
    op, build = operator_env.operator, operator_env.build

    status, body = op.activate(build)

    assert status == 200, body
    assert body["status"] == "ok"
    assert body["current_updated"] is True
    assert runtime.active() == build, "current must name the build that was just activated"


def test_a_failed_health_check_leaves_current_alone(operator_env, monkeypatch):
    """The failure direction: no partial flip. The old build stays active in BOTH places."""
    op, build = operator_env.operator, operator_env.build
    before = runtime.active()

    monkeypatch.setattr("router.operator._health_check", lambda *a, **kw: False)

    with pytest.raises(Exception):
        op.activate(build)

    assert runtime.active() == before, "a refused activation must not move the pointer"


def test_activation_still_succeeds_when_the_symlink_cannot_be_written(operator_env, monkeypatch):
    """The promotion is already committed in the store by then. Reporting failure for work that
    succeeded would be a lie, and the next startup repairs the pointer anyway."""
    op, build = operator_env.operator, operator_env.build

    def _boom(_build):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("router.operator.runtime_activate", _boom)

    status, body = op.activate(build)

    assert status == 200
    assert body["status"] == "ok"
    assert body["current_updated"] is False, "the caller must be able to see the cache lagged"
