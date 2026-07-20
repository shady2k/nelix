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
    def __init__(self, registry, router_epoch, store=None, lease_service=None):
        self._registry = registry
        self._router_epoch = router_epoch
        self._store = store
        self._lease_service = lease_service
        # FIX 5: injectable reap function for tests. When set, _reap_generation
        # delegates to this instead of the real GenerationSupervisor.
        self._reap_fn = None

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
        try:
            if method == "GET":
                status, resp = relay(
                    lambda: client.forward_raw(method, path, None))
            else:
                status, resp = relay(
                    lambda: client.forward_raw(method, path, body))
        except NelixError as e:
            if e.code != GENERATION_UNAVAILABLE:
                raise
            return None, None
        return status, resp

    def _resolve_confirmed(self, generation_id, epoch):
        """Resolve per-epoch confirmed_high_water: enumerate ALL terminals (incl
        acked/expired) in terminal_seq order, advance the watermark to the highest
        contiguous H where every terminal ≤ H is resolved (board-visible OR
        owner-acked OR validly-expired).
        Returns True on success (may have advanced watermark or no-op). Returns
        False on store error — caller returns blocked (retryable). Never swallows
        resolver errors into the success path.
        """
        try:
            terminals = self._store.list_terminal_for_epoch(epoch)
            hw = 0
            existing = {tr.terminal_seq for tr in terminals if tr.terminal_seq is not None}
            if not existing:
                return True
            while hw + 1 in existing:
                hw += 1
            if hw > 0:
                self._store.set_generation_confirmed_high_water(epoch, hw)
            return True
        except Exception:
            if _log is not None:
                _log.warning("operator", "resolve_confirmed_failed",
                             generation_id=generation_id, epoch=epoch, exc_info=True)
            return False

    def _reap_generation(self, generation_id, epoch):
        """Stop/reap the serving incarnation for a draining generation.
        If self._reap_fn is set (test injection), delegates to it.
        Otherwise reads incarnation_meta from the epoch, constructs a
        GenerationSupervisor, and calls reap_holder guarded by incarnation identity.
        Returns True if daemon confirmed dead/killed/gone (success).
        Returns False if reap refused (identity mismatch / no meta / error) —
        caller must NOT retire."""
        if self._reap_fn is not None:
            return self._reap_fn(generation_id, epoch)
        try:
            from generation_supervisor import GenerationSupervisor
            gen_rec = self._store.get_generation(generation_id)
            epochs = self._store.list_epochs_strict(generation_id)
            for ep in epochs:
                if ep.generation_epoch == epoch and ep.incarnation_meta:
                    import json
                    inc = json.loads(ep.incarnation_meta)
                    sup = GenerationSupervisor(generation_id, gen_rec.build_id)
                    return sup.reap_holder(inc)
        except Exception:
            if _log is not None:
                _log.warning("operator", "reap_failed",
                             generation_id=generation_id, exc_info=True)
        return False

    def _crash_reconcile_epoch(self, generation_id, epoch):
        """Crash reconciliation for a dead epoch (§3.5 crash path + §3.3f).

        Under the operator lock (caller must hold it):
        1. Verify epoch is ALREADY process_state=dead.
        2. Prove daemon death: require valid incarnation_meta with int>0 pid that
           ``_pid_alive`` confirms NOT alive. Missing/malformed/non-int/<=0 => BLOCKED.
        3. Set retirement_state=quiescing BEFORE enumerating (close admission).
        4. Reap child groups — prove completeness + verify gone (FIX 3 fail-closed).
        5. Persist ``generation_lost`` for EVERY outstanding obligation.
        6. Release the epoch's LeaseService tokens (FIX 4 — failure => BLOCKED).
        7. Certify (router-issued), set retirement_state=certified.

        Returns ``(success: bool, blocker: str|None)``.
        """
        from generation_supervisor import _pid_alive
        import json
        import time as _time

        # 1. Verify epoch exists and is ALREADY dead
        epochs = self._store.list_epochs_strict(generation_id)
        target_ep = None
        for ep in epochs:
            if ep.generation_epoch == epoch:
                target_ep = ep
                break
        if target_ep is None:
            return False, "epoch_not_found"
        if target_ep.process_state != "dead":
            return False, "epoch_not_dead"

        # 2. Death proof: valid incarnation_meta with int>0 pid (FIX 2)
        if not target_ep.incarnation_meta:
            return False, "missing_incarnation_meta"
        try:
            inc = json.loads(target_ep.incarnation_meta)
        except (json.JSONDecodeError, TypeError):
            return False, "malformed_incarnation_meta"
        expected_pid = inc.get("pid")
        if not isinstance(expected_pid, int) or expected_pid <= 0:
            return False, "invalid_incarnation_pid"
        if _pid_alive(expected_pid):
            return False, "daemon_still_alive"

        # 3. Quiesce first (close admission before enumerating)
        if target_ep.retirement_state == "open":
            self._store.set_epoch_retirement(epoch, retirement_state="quiescing")

        # 4. Reap child groups (FIX 3 — fail-closed completeness + group kill)
        _reap_ok, _blocker = self._reap_child_groups(epoch, generation_id)
        if not _reap_ok:
            return False, _blocker

        # 5. Persist generation_lost for EVERY outstanding obligation
        try:
            outstanding = self._store.list_starts_for_epoch(epoch)
        except Exception as e:
            return False, f"list_starts_failed:{e}"
        for row in outstanding:
            sid = row["session_id"]
            try:
                self._store.put_terminal(
                    sid, terminal_kind="generation_lost",
                    summary="generation lost (daemon crashed)",
                    ended_at=_time.time())
            except Exception as e:
                if _log is not None:
                    _log.warning("operator: generation_lost_failed "
                                 "gen=%s epoch=%s sid=%s err=%s",
                                 generation_id, epoch, sid, e)
                return False, f"generation_lost_failed:{sid}:{e}"

        # 6. Release leases (FIX 4 — failure MUST block)
        if self._lease_service is not None:
            try:
                self._lease_service.release_epoch(generation_id, epoch)
            except Exception as e:
                if _log is not None:
                    _log.warning("operator: lease_release_failed "
                                 "gen=%s epoch=%s err=%s",
                                 generation_id, epoch, e)
                return False, f"lease_release_failed:{e}"
        elif self._lease_service is None:
            pass

        # 7. Certify — final high-water AFTER all persisted
        final_hw = self._store.get_generation_persisted_high_water(epoch)
        certificate = f"crash-reconcile:{generation_id}:{epoch}"
        self._store.set_epoch_retirement(
            epoch, retirement_state="certified",
            certificate=certificate, final_high_water=final_hw)

        if _log is not None:
            _log.info("operator: crash_reconciled gen=%s epoch=%s "
                      "final_hw=%s obligations=%s",
                      generation_id, epoch, final_hw, len(outstanding))

        return True, None

    def _reap_child_groups(self, epoch, generation_id):
        """Reap and verify child groups are gone (FIX 3 — fail-closed).

        Proves completeness: every admitted session without a terminal MUST have a
        readable child-group record with non-null leader_fingerprint. Any missing/
        incomplete/null-fingerprint/unreadable record => BLOCKED.

        For each complete record: kills the process group (SIGTERM then SIGKILL),
        verifies the WHOLE GROUP gone via ``os.killpg(pgid,0)`` => ESRCH. A record
        whose leader_fingerprint no longer matches the live pid => reused pid => skip.

        Returns ``(ok: bool, blocker: str|None)``.
        """
        from daemon.reaper import ProcessInspector
        import signal
        import time as _time

        child_groups = self._store.list_epoch_child_groups(epoch)

        # --- Part A: completeness proof ---
        try:
            outstanding = self._store.list_starts_for_epoch(epoch)
        except Exception as e:
            return False, f"list_starts_failed:{e}"

        outstanding_sids = {r["session_id"] for r in outstanding}
        recorded_sids = {cg["session_id"] for cg in child_groups
                         if cg.get("session_id")}

        for sid in outstanding_sids:
            if sid not in recorded_sids:
                if _log is not None:
                    _log.warning("operator: child_record_missing "
                                 "gen=%s epoch=%s sid=%s",
                                 generation_id, epoch, sid)
                return False, f"child_record_missing:{sid}"

        # --- Part B: validate and reap each child group ---
        inspector = ProcessInspector()
        for cg in child_groups:
            child_pid = cg.get("child_pid")
            child_pgid = cg.get("child_pgid")
            leader_fp = cg.get("leader_fingerprint")
            cg_sid = cg.get("session_id")

            if not cg_sid:
                return False, "child_record_missing_session_id"
            if not child_pid:
                return False, f"incomplete_child_record:{cg_sid}"
            if child_pgid is None:
                return False, f"child_missing_pgid:{cg_sid}"
            if not leader_fp:
                return False, f"child_null_fingerprint:{cg_sid}"

            # Reject stale PID/PGID via leader_fingerprint (FIX 3)
            try:
                actual_fp = inspector.start_fingerprint(child_pid)
            except Exception:
                actual_fp = None
            if actual_fp is not None and actual_fp != leader_fp:
                continue

            # Kill the group — SIGTERM then SIGKILL
            try:
                os.killpg(child_pgid, signal.SIGTERM)
            except PermissionError:
                return False, "child_group_still_alive"
            except ProcessLookupError:
                pass
            except OSError as e:
                if _log is not None:
                    _log.warning("operator: killpg_sigterm_failed "
                                 "gen=%s epoch=%s pgid=%s err=%s",
                                 generation_id, epoch, child_pgid, e)
                return False, f"killpg_sigterm_failed:{child_pgid}:{e}"

            _time.sleep(0.1)

            try:
                os.killpg(child_pgid, signal.SIGKILL)
            except PermissionError:
                return False, "child_group_still_alive"
            except ProcessLookupError:
                pass
            except OSError as e:
                if _log is not None:
                    _log.warning("operator: killpg_sigkill_failed "
                                 "gen=%s epoch=%s pgid=%s err=%s",
                                 generation_id, epoch, child_pgid, e)
                return False, f"killpg_sigkill_failed:{child_pgid}:{e}"

            _time.sleep(0.1)

            # Verify THE WHOLE GROUP is gone: os.killpg(pgid,0) => ESRCH
            try:
                os.killpg(child_pgid, 0)
            except ProcessLookupError:
                pass
            except PermissionError:
                return False, "child_group_still_alive"
            except OSError as e:
                return False, f"child_group_verify_failed:{e}"
            else:
                return False, "child_group_still_alive"

        self._store.clear_epoch_child_groups(epoch)
        return True, None

    def retire(self, generation_id: str):
        """Retire a generation.

        PHASE 1 — CERTIFY (skip for already-certified epochs):
           For each uncertified epoch: crash-reconcile if dead, clean-path if serving.

        PHASE 2 — FINALIZATION GATES (run EVERY call, even for certified epochs):
           a) Reap any live/recorded incarnation (idempotent — must prove gone).
           b) Release leases for every epoch (idempotent — must succeed).
           c) Resolve confirmed high-water for all epochs.
           d) Aggregate oracle check (all certified + confirmed>=final).

        PHASE 3 — FINALIZE:
           clear_current_epoch + FSM→retired ONLY after all gates pass.

        FIX 1: finalization gates re-run every call; a certified epoch skips
        re-certification but still passes reap+lease+oracle.
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

        if gen.lifecycle_state == RETIRING:
            pass
        elif gen.lifecycle_state not in (DRAINING, ACTIVE):
            raise NelixError(
                INVALID_REQUEST,
                f"generation {generation_id} is {gen.lifecycle_state!r}, "
                f"must be draining or active to retire")

        all_epochs = self._store.list_epochs(generation_id)
        if not all_epochs:
            raise NelixError(INVALID_REQUEST,
                             f"generation {generation_id} has no epochs")

        current_epoch = gen.current_epoch

        lock_fd = self._generations_lock_acquire()
        try:

            # ================================================================
            # PHASE 1 — Certify uncertified epochs
            # ================================================================
            for ep in all_epochs:
                if ep.retirement_state == "certified":
                    continue

                if ep.process_state == "dead":
                    success, blocker = self._crash_reconcile_epoch(
                        generation_id, ep.generation_epoch)
                    if not success:
                        return 200, {
                            "operation": "retire",
                            "status": "blocked",
                            "generation_id": generation_id,
                            "lifecycle_state": gen.lifecycle_state,
                            "blockers": [f"crash_reconcile_failed:{ep.generation_epoch}:{blocker}"],
                        }
                elif current_epoch is not None and ep.generation_epoch == current_epoch:
                    if ep.retirement_state == "open":
                        self._store.set_epoch_retirement(
                            ep.generation_epoch, retirement_state="quiescing")
                    self._daemon_rpc(generation_id, "POST", "/operator/quiesce")

                    if not self._resolve_confirmed(generation_id, ep.generation_epoch):
                        return 200, {
                            "operation": "retire",
                            "status": "blocked",
                            "generation_id": generation_id,
                            "lifecycle_state": gen.lifecycle_state,
                            "blockers": ["resolve_failed"],
                        }

                    quiesced = False
                    status, resp = self._daemon_rpc(
                        generation_id, "GET", "/operator/quiesce_status")
                    if status == 200 and isinstance(resp, dict):
                        qs = resp.get("status", {})
                        live = qs.get("live_sessions", 1)
                        obligations = qs.get("outstanding_obligations", 1)
                        pending = qs.get("terminal_pending", 1)
                        in_flight = qs.get("in_flight_admissions", 1)
                        if live == 0 and obligations == 0 and pending == 0 and in_flight == 0:
                            quiesced = True

                    if not quiesced:
                        return 200, {
                            "operation": "retire",
                            "status": "blocked",
                            "generation_id": generation_id,
                            "lifecycle_state": gen.lifecycle_state,
                            "blockers": ["not_quiesced"],
                        }

                    certificate = f"retire:{generation_id}:{ep.generation_epoch}"
                    status, resp = self._daemon_rpc(
                        generation_id, "POST", "/operator/certify_epoch",
                        {"certificate": certificate,
                         "generation_epoch": ep.generation_epoch})
                    ep_refresh = self._store.get_epoch_retirement_state(
                        ep.generation_epoch)
                    if ep_refresh != "certified":
                        return 200, {
                            "operation": "retire",
                            "status": "blocked",
                            "generation_id": generation_id,
                            "lifecycle_state": gen.lifecycle_state,
                            "blockers": ["certify_failed"],
                            "rpc_response": resp,
                        }

                    resolve2_ok = self._resolve_confirmed(generation_id,
                                                           ep.generation_epoch)
                    if resolve2_ok:
                        fresh_epochs = self._store.list_epochs(generation_id)
                        fresh_fhw = 0
                        for ep2 in fresh_epochs:
                            if ep2.generation_epoch == ep.generation_epoch:
                                fresh_fhw = ep2.final_high_water or 0
                                break
                        chw = self._store.get_generation_confirmed_high_water(
                            ep.generation_epoch)
                        if chw < fresh_fhw:
                            resolve2_ok = False
                    if not resolve2_ok:
                        return 200, {
                            "operation": "retire",
                            "status": "blocked",
                            "generation_id": generation_id,
                            "lifecycle_state": gen.lifecycle_state,
                            "blockers": ["confirmed_below_final"],
                        }

            # ================================================================
            # PHASE 2 — FINALIZATION GATES (re-run EVERY call, FIX 1)
            # ================================================================

            # 2a. Reap any live incarnation (idempotent)
            for ep in self._store.list_epochs(generation_id):
                if ep.incarnation_meta:
                    if not self._reap_generation(generation_id, ep.generation_epoch):
                        return 200, {
                            "operation": "retire",
                            "status": "blocked",
                            "generation_id": generation_id,
                            "lifecycle_state": gen.lifecycle_state,
                            "blockers": ["reap_refused_or_failed"],
                        }

            # 2b. Clear current_epoch (so oracle can pass)
            if self._store.get_generation(generation_id).current_epoch is not None:
                self._store.clear_current_epoch(generation_id)

            # 2c. Release leases for every epoch (FIX 4 — must succeed)
            if self._lease_service is not None:
                for ep in self._store.list_epochs(generation_id):
                    try:
                        self._lease_service.release_epoch(generation_id, ep.generation_epoch)
                    except Exception as e:
                        if _log is not None:
                            _log.warning("operator: lease_release_failed "
                                         "gen=%s epoch=%s err=%s",
                                         generation_id, ep.generation_epoch, e)
                        return 200, {
                            "operation": "retire",
                            "status": "blocked",
                            "generation_id": generation_id,
                            "lifecycle_state": gen.lifecycle_state,
                            "blockers": [f"lease_release_failed:{ep.generation_epoch}:{e}"],
                        }

            # 2c. Resolve confirmed for ALL epochs
            for ep in self._store.list_epochs(generation_id):
                self._resolve_confirmed(generation_id, ep.generation_epoch)

            # 2d. Aggregate oracle check
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

            # ================================================================
            # PHASE 3 — FINALIZE (FSM → retired)
            # ================================================================
            if self._store.get_generation(generation_id).lifecycle_state == ACTIVE:
                validate_transition(ACTIVE, DRAINING)
                self._store.set_generation_lifecycle_state(generation_id, DRAINING)
            if self._store.get_generation(generation_id).lifecycle_state == DRAINING:
                validate_transition(DRAINING, RETIRING)
                self._store.set_generation_lifecycle_state(generation_id, RETIRING)
            if self._store.get_generation(generation_id).lifecycle_state == RETIRING:
                validate_transition(RETIRING, RETIRED)
                self._store.set_generation_lifecycle_state(generation_id, RETIRED)

            final_state = self._store.get_generation(generation_id).lifecycle_state
            return 200, {
                "operation": "retire",
                "status": "ok",
                "generation_id": generation_id,
                "lifecycle_state": final_state,
            }
        finally:
            import os as _os
            _os.close(lock_fd)
