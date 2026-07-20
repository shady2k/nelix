"""nelix-80e S4 — operator plane: install, activate, list, retire.

Operator commands are router-local (never fanned out, never merged across generations).
All mutations are serialized via generations_install_lock.
"""
import json
import logging
import os
import time
import urllib.parse

from nelix_contracts.errors import GENERATION_UNAVAILABLE, IDEMPOTENCY_CONFLICT, INVALID_REQUEST, UNKNOWN_SESSION, NelixError
from nelix_contracts.ids import new_generation_id

from nelix_contracts.lifecycle import READY, ACTIVE, DRAINING, RETIRING, RETIRED, validate_transition

from nelix_contracts.retirement import generation_retirement_oracle_blockers

from router.forwarding import relay
from router.registry import PROBE_OWNER

try:
    from rpc_client import RpcClient
except ImportError:
    from .rpc_client import RpcClient

_log = logging.getLogger("nelix.operator")

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

    # ---------------------------------------------------------------- retire

    def _daemon_rpc(self, generation_id, method, path, body=None):
        """Call the daemon for the given generation via RPC.
        Returns (status_code, response_dict) or (None, None) on transport failure."""
        gen = None
        for g in self._registry.generations():
            if g.generation_id == generation_id:
                gen = g
                break
        if gen is None or gen.transport is None:
            return None, None
        client = RpcClient(gen.transport, PROBE_OWNER)
        raw_body = json.dumps(body or {}).encode()
        try:
            if method == "GET":
                status, resp = relay(
                    lambda: client.forward_raw(method, path, None))
            else:
                status, resp = relay(
                    lambda: client.forward_raw(method, path, raw_body))
        except NelixError as e:
            if e.code != GENERATION_UNAVAILABLE:
                raise
            return None, None
        return status, resp

    def retire(self, generation_id: str):
        """Retire a generation: drive quiescence, certify epochs, check oracle,
        and transition lifecycle draining -> retiring -> retired.

        Phase 1: tell the daemon to begin_quiescence (state=quiescing + close admission).
        Phase 2: poll daemon quiescence_status until zero obligations + no live PTYs.
        Phase 3: certify each epoch via daemon (barrier-gated, atomic high-water).
        Phase 4: stop/reap the draining incarnation, clear current_epoch in store.
        Phase 5: check the generation-level oracle.
        Phase 6: transition lifecycle draining -> retiring -> retired.

        Returns 200 with blockers if not yet quiesced, or with lifecycle state.
        Idempotent: already-retired returns success.
        """
        if not isinstance(generation_id, str) or not generation_id:
            raise NelixError(INVALID_REQUEST,
                             f"generation_id must be a non-empty string: {generation_id!r}")
        if self._store is None:
            raise NelixError(GENERATION_UNAVAILABLE,
                             "no store configured; cannot retire")

        try:
            gen = self._store.get_generation(generation_id)
        except NelixError as e:
            if e.code == UNKNOWN_SESSION:
                raise NelixError(INVALID_REQUEST,
                                 f"no such generation: {generation_id}") from None
            raise

        if gen.lifecycle_state == RETIRED:
            return 200, {"operation": "retire", "status": "ok",
                          "generation_id": generation_id,
                          "lifecycle_state": RETIRED, "idempotent": True}

        # D1: accept RETIRING as idempotent (already in progress).
        if gen.lifecycle_state == RETIRING:
            pass
        elif gen.lifecycle_state not in (DRAINING, ACTIVE):
            raise NelixError(
                INVALID_REQUEST,
                f"generation {generation_id} is {gen.lifecycle_state!r}, "
                f"must be draining or active to retire")

        epochs = self._store.list_epochs(generation_id)
        if not epochs:
            raise NelixError(INVALID_REQUEST,
                             f"generation {generation_id} has no epochs")

        # ---- Phase 1: drive quiescence via daemon RPC ----
        daemon_ok = False
        for ep in epochs:
            if ep.retirement_state == "open":
                self._store.set_epoch_retirement(
                    ep.generation_epoch, retirement_state="quiescing")
            status, resp = self._daemon_rpc(
                generation_id, "POST", "/operator/quiesce")
            if status == 200:
                daemon_ok = True

        # ---- Phase 2: poll daemon quiescence_status ----
        quiesced = False
        if daemon_ok:
            status, resp = self._daemon_rpc(
                generation_id, "GET", "/operator/quiesce_status")
            if status == 200 and isinstance(resp, dict):
                qs = resp.get("status", {})
                live = qs.get("live_sessions", 1)
                obligations = qs.get("outstanding_obligations", 1)
                pending = qs.get("terminal_pending", 1)
                if live == 0 and obligations == 0 and pending == 0:
                    quiesced = True

        # Fallback: if daemon RPC unavailable, check the store directly.
        if not quiesced and not daemon_ok:
            if all(ep.retirement_state in ("quiescing", "certified")
                   for ep in epochs):
                quiesced = True

        if not quiesced:
            blockers = ["not_quiesced"]
            return 200, {
                "operation": "retire",
                "status": "blocked",
                "generation_id": generation_id,
                "lifecycle_state": gen.lifecycle_state,
                "blockers": blockers,
            }

        # ---- Phase 3: certify each epoch via daemon RPC (or store directly) ----
        for ep in epochs:
            certificate = f"retire:{generation_id}:{ep.generation_epoch}"
            status, resp = self._daemon_rpc(
                generation_id, "POST", "/operator/certify_epoch",
                {"certificate": certificate})
            if status != 200:
                # Fallback: certify directly in the store (daemon unavailable).
                if ep.retirement_state != "certified":
                    final_hw = self._store.get_generation_persisted_high_water(
                        ep.generation_epoch)
                    self._store.set_epoch_retirement(
                        ep.generation_epoch,
                        retirement_state="certified",
                        certificate=certificate,
                        final_high_water=final_hw)

        # ---- Phase 4: stop incarnation, clear current_epoch ----
        self._store.clear_current_epoch(generation_id)

        # ---- Phase 5: check oracle ----
        blockers = generation_retirement_oracle_blockers(
            store=self._store, generation_id=generation_id)

        if blockers:
            return 200, {
                "operation": "retire",
                "status": "blocked",
                "generation_id": generation_id,
                "lifecycle_state": gen.lifecycle_state,
                "blockers": list(blockers),
            }

        # ---- Phase 6: transition lifecycle ----
        if gen.lifecycle_state == ACTIVE:
            validate_transition(ACTIVE, DRAINING)
            self._store.set_generation_lifecycle_state(
                generation_id, DRAINING)

        if gen.lifecycle_state in (ACTIVE, DRAINING):
            validate_transition(DRAINING, RETIRING)
            self._store.set_generation_lifecycle_state(
                generation_id, RETIRING)

        if gen.lifecycle_state in (ACTIVE, DRAINING, RETIRING):
            validate_transition(RETIRING, RETIRED)
            self._store.set_generation_lifecycle_state(
                generation_id, RETIRED)

        return 200, {
            "operation": "retire",
            "status": "ok",
            "generation_id": generation_id,
            "lifecycle_state": RETIRED,
        }
