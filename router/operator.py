"""nelix-80e S4 — operator plane: install, activate, list, retire.

Operator commands are router-local (never fanned out, never merged across generations).
All mutations are serialized via generations_install_lock.
"""
import json
import logging
import os
import time
import urllib.parse

from nelix_contracts.errors import GENERATION_UNAVAILABLE, IDEMPOTENCY_CONFLICT, INVALID_REQUEST, NelixError
from nelix_contracts.ids import new_generation_id

from nelix_contracts.lifecycle import READY, ACTIVE, DRAINING, validate_transition

from router.forwarding import relay
from router.registry import PROBE_OWNER

try:
    from rpc_client import RpcClient
except ImportError:
    from .rpc_client import RpcClient

_log = logging.getLogger("nelix.operator")

_ERR_S5 = "retire is not implemented until S5"
_ACTIVATE_HEALTH_RETRIES = 3
_ACTIVATE_HEALTH_DELAY = 1.0


def _ensure_dirs(sup):
    """Ensure generation runtime dirs exist."""
    sup.ensure_generation_dirs()


def _health_check(sup, transport, epoch, gid, build_id) -> bool:
    """Health-check the identity triple with retries."""
    for i in range(_ACTIVATE_HEALTH_RETRIES):
        if sup._check_health_strict(transport, epoch, gid, build_id):
            return True
        if i < _ACTIVATE_HEALTH_RETRIES - 1:
            import time as _time
            _time.sleep(_ACTIVATE_HEALTH_DELAY)
    return False


class OperatorRoutes:
    def __init__(self, registry, router_epoch, store=None):
        self._registry = registry
        self._router_epoch = router_epoch
        self._store = store

    def generation_list(self):
        """The registry's topology (size 1 today): each tracked generation's router-minted
        generation_id, build_id, and transport kind."""
        gens = self._registry.generations()
        return 200, {
            "router_epoch": self._router_epoch,
            "generations": [
                {"generation_id": g.generation_id, "build_id": g.build_id,
                 "transport_kind": getattr(g.transport, "kind", None)}
                for g in gens
            ],
        }

    def capabilities(self):
        """Minimal + honest: the router's own identity + the one active generation's real
        global /capabilities baseline, forwarded verbatim."""
        gens = self._registry.generations()
        if not gens:
            raise NelixError(GENERATION_UNAVAILABLE, "no generation is currently available")
        gen = gens[0]
        client = RpcClient(gen.transport, PROBE_OWNER)
        path = "/capabilities?" + urllib.parse.urlencode({"owner_id": PROBE_OWNER})
        status, body = relay(lambda: client.forward_raw("GET", path, None))
        return status, {"router_epoch": self._router_epoch, "generation_id": gen.generation_id,
                        "capabilities": body}

    def _generations_lock_acquire(self):
        """Acquire the generations lock for serialization."""
        from daemon import singleton
        import paths
        lock_path = paths.generations_install_lock()
        fd = singleton.acquire(lock_path, {"pid": os.getpid(), "op": "operator"})
        if fd is None:
            raise NelixError(IDEMPOTENCY_CONFLICT,
                             "another operator operation is in progress; try again")
        return fd

    # ---------------------------------------------------------------- install

    def install(self, wheel_path: str):
        """Install a wheel and return its build_id. Idempotent: if the build
        is already installed, returns the same build_id."""
        from runtime import install as runtime_install
        build_id = runtime_install(wheel_path)
        return 200, {"operation": "install", "status": "installed",
                      "build_id": build_id}

    # ---------------------------------------------------------------- activate

    def activate(self, build_id: str):
        """Activate a build: create a new generation+epoch, spawn, health-check,
        atomically flip old->draining + new->active, adopt into registry.

        Idempotent: re-activating the already-active build_id is a no-op success.
        On health-check failure: the new epoch is reconciled dead, the old stays
        active, and an error is returned (no partial flip).
        """
        if not isinstance(build_id, str) or not build_id:
            raise NelixError(INVALID_REQUEST,
                             f"build_id must be a non-empty string: {build_id!r}")

        if self._store is None:
            raise NelixError(GENERATION_UNAVAILABLE,
                             "no store configured; cannot activate")

        # Lock for serialization.
        fd = self._generations_lock_acquire()
        try:
            return self._activate_locked(build_id)
        finally:
            if fd is not None:
                os.close(fd)

    def _activate_locked(self, build_id: str):
        from runtime import is_installed
        if not is_installed(build_id):
            raise NelixError(INVALID_REQUEST,
                             f"build {build_id} is not installed")

        # Check if this build is already active — idempotent no-op.
        try:
            current_active = self._registry.active()
            if current_active.build_id == build_id:
                return 200, {"operation": "activate", "status": "ok",
                             "generation_id": current_active.generation_id,
                             "build_id": build_id, "idempotent": True}
        except NelixError:
            pass

        clock = time.time()

        # Find the current active generation (if any).
        old_gen = None
        try:
            existing_gens = self._store.list_generations()
            active_rows = [g for g in existing_gens
                           if g.lifecycle_state == "active"]
            if active_rows:
                old_gen = active_rows[0]
        except NelixError:
            pass

        # Mint new generation + epoch.
        new_gid = new_generation_id()
        self._store.create_generation(
            new_gid, build_id=build_id,
            lifecycle_state=READY,
            capability_snapshot=None, created_at=clock)
        new_epoch = new_generation_id()
        self._store.insert_epoch(
            new_epoch, new_gid, incarnation_meta=None, created_at=clock)

        # Spawn daemon via supervisor.
        from generation_supervisor import GenerationSupervisor
        sup = GenerationSupervisor(new_gid, build_id)
        _ensure_dirs(sup)

        incarnation = None
        transport = None
        try:
            incarnation, transport = sup.ensure_running(new_epoch)
        except Exception as e:
            self._store.reconcile_epoch_dead(new_gid, new_epoch)
            raise NelixError(GENERATION_UNAVAILABLE,
                             f"failed to spawn generation daemon: {e}") from None

        # Health-check the identity triple.
        if not _health_check(sup, transport, new_epoch, new_gid, build_id):
            if incarnation:
                sup.reap_holder(incarnation)
            self._store.reconcile_epoch_dead(new_gid, new_epoch)
            raise NelixError(GENERATION_UNAVAILABLE,
                             "generation health check failed (identity triple)")

        # Re-read holder fingerprint after health check.
        holder = sup._live_lock_holder()
        if not holder:
            sup.reap_holder(incarnation)
            self._store.reconcile_epoch_dead(new_gid, new_epoch)
            raise NelixError(GENERATION_UNAVAILABLE,
                             "generation daemon vanished before promotion")

        current_inc = {"pid": holder["pid"],
                       "start_fingerprint": holder.get("start_fingerprint")}

        # Promote epoch to serving.
        try:
            self._store.cas_epoch_serving(
                new_gid, new_epoch, expected_current_epoch=None,
                incarnation_meta=json.dumps(current_inc, sort_keys=True))
        except NelixError:
            sup.reap_holder(current_inc)
            self._store.reconcile_epoch_dead(new_gid, new_epoch)
            raise

        # ATOMIC FLIP: old->draining + new->active in one store transaction.
        if old_gen is not None:
            validate_transition(old_gen.lifecycle_state, DRAINING)
            self._store.set_generation_lifecycle_state_atomic(
                old_gen.generation_id, new_gid,
                new_state_old=DRAINING,
                expected_old_state=ACTIVE,
                expected_new_state=READY)
        else:
            validate_transition(READY, ACTIVE)
            self._store.set_generation_lifecycle_state_atomic(
                new_gid, new_gid,
                new_state_old=ACTIVE,
                expected_old_state=READY,
                expected_new_state=READY)

        # Adopt into registry and bump topology revision.
        self._registry.adopt_generation(new_gid, new_epoch, transport, build_id,
                                         incarnation=current_inc)

        return 200, {"operation": "activate", "status": "ok",
                      "generation_id": new_gid, "build_id": build_id,
                      "epoch": new_epoch}

    # ---------------------------------------------------------------- list

    def list(self):
        """Return all generations with lifecycle states + current epochs."""
        if self._store is not None:
            gens = self._store.list_generations()
        else:
            gens = []
        out = []
        for g in gens:
            entry = {
                "generation_id": g.generation_id,
                "build_id": g.build_id,
                "lifecycle_state": g.lifecycle_state,
                "current_epoch": g.current_epoch,
                "created_at": g.created_at,
            }
            out.append(entry)
        return 200, {
            "router_epoch": self._router_epoch,
            "generations": out,
        }

    # ---------------------------------------------------------------- retire (disabled)

    def retire(self, generation_id: str):
        """Feature-disabled until S5. Returns a clear error."""
        raise NelixError(INVALID_REQUEST, _ERR_S5)
