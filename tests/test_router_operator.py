"""nelix-3rm slice 3c.2 Part C: OperatorRoutes — capabilities + generation_list.

Both are routing.OPERATOR: router-LOCAL, never fanned out (classify()'s docstring: fanning
capabilities out would merge N generations' answers into one, defeating a per-session capability
check; the topology read never needs one either). The router owns both response shapes. These
tests assert the ONE tracked generation's REAL facts come back — not fabricated, not merged."""
import pytest

from router.registry import GenerationRegistry
from router.operator import OperatorRoutes

from tests._router_fakes import Backend, Supervisor

_EPOCH = "r-" + "0" * 32


@pytest.fixture
def wired():
    backend = Backend(build_id="b-real-1")
    registry = GenerationRegistry(supervisor=Supervisor(backend.transport),
                                  health_probe=lambda t: backend.build_id)
    ops = OperatorRoutes(registry, _EPOCH)
    yield ops, registry, backend
    backend.close()


def test_generation_list_is_empty_before_anything_is_observed():
    # No /health probe has run yet (mirrors router /health's own "must not spawn" contract) --
    # registry.generations() is a snapshot (it never touches the supervisor), so an untouched
    # registry reports no generations regardless of what supervisor it holds.
    registry = GenerationRegistry(supervisor=object())
    ops = OperatorRoutes(registry, _EPOCH)
    status, body = ops.generation_list()
    assert status == 200
    assert body == {"router_epoch": _EPOCH, "generations": []}


def test_generation_list_reports_the_one_active_generation(wired):
    ops, registry, backend = wired
    registry.active()                          # observe it once (mirrors a prior /start)
    status, body = ops.generation_list()
    assert status == 200
    assert body["router_epoch"] == _EPOCH
    assert len(body["generations"]) == 1
    g = body["generations"][0]
    assert g["generation_id"] == registry.active().epoch
    assert g["build_id"] == "b-real-1"
    assert g["transport_kind"] == "tcp"


def test_capabilities_forwards_the_generations_global_baseline(wired):
    ops, registry, backend = wired
    registry.active()                           # observe it once (mirrors a prior /start or /health)
    status, body = ops.capabilities()
    assert status == 200
    assert body["router_epoch"] == _EPOCH
    assert body["generation_id"] == registry.active().epoch
    assert body["capabilities"]["executors"]["demo"]["hook_capable"] is True


def test_capabilities_probe_never_carries_a_real_owner(wired):
    # /capabilities requires SOME owner_id on the wire (daemon/rpc_server.py), but this call has no
    # real caller to source one from -- it must use the registry's own no-owner probe identity
    # (mirrors the /health build-id probe), never fabricate/borrow a caller's owner_id.
    ops, registry, backend = wired
    registry.active()                           # observe it once (mirrors a prior /start or /health)
    ops.capabilities()
    call = backend.calls[-1]
    assert call["path"].startswith("/capabilities")
    from router.registry import PROBE_OWNER
    assert call["query"]["owner_id"] == [PROBE_OWNER]


def test_capabilities_transport_failure_is_retryable_generation_unavailable():
    from daemon.transport import Transport
    from nelix_contracts.errors import NelixError

    class _DeadSupervisor:
        _t = Transport.tcp("127.0.0.1", 9, "t")

        def active_generation(self):
            return (self._t, {"pid": 1, "start_fingerprint": "fp"})

        def held_generation(self):
            return (self._t, {"pid": 1, "start_fingerprint": "fp"})

        def ensure_running(self):
            return self._t

    registry = GenerationRegistry(supervisor=_DeadSupervisor(), health_probe=lambda t: None)
    registry.active()                           # observe the (unreachable) generation once
    ops = OperatorRoutes(registry, _EPOCH)
    with pytest.raises(NelixError) as exc:
        ops.capabilities()
    assert exc.value.code == "generation_unavailable"
    assert exc.value.retryable is True


def test_capabilities_is_non_spawning_when_no_generation_is_recorded():
    # Finding: GET /capabilities must be as non-spawning as /health and /generation_list -- it must
    # NEVER call registry.active() (which can subprocess.Popen a daemon). Before anything has
    # observed a generation (no prior /start, no /health probe), it must answer the same retryable
    # GENERATION_UNAVAILABLE /health's absent active_generation implies, and must never touch the
    # supervisor at all (a touch would mean it tried to make one available -- i.e. spawn).
    class _BoomSupervisor:
        def active_generation(self):
            raise AssertionError("capabilities must never probe for a generation to spawn one")

        def ensure_running(self):
            raise AssertionError("capabilities must never spawn a generation")

        def held_generation(self):
            raise AssertionError("capabilities must never read the lock holder to spawn one")

    from nelix_contracts.errors import NelixError

    registry = GenerationRegistry(supervisor=_BoomSupervisor())
    ops = OperatorRoutes(registry, _EPOCH)
    with pytest.raises(NelixError) as exc:
        ops.capabilities()
    assert exc.value.code == "generation_unavailable"
    assert exc.value.retryable is True
