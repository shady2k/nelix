"""The router's generation registry — ONE generation today, structurally multi-generation.

S1c-2 (authority switch): production uses per-generation GenerationSupervisor, mint-before-spawn,
single-flight, incarnation-guarded reap, eager re-adoption in constructor.
"""
import json
import logging
import threading
import time
from dataclasses import dataclass

from nelix_contracts.errors import GENERATION_UNAVAILABLE, IDEMPOTENCY_CONFLICT, UNKNOWN_SESSION, NelixError
from nelix_contracts.ids import new_generation_id

from generation_supervisor import GenerationSupervisor

PROBE_OWNER = "nelix-router-probe"

_log = logging.getLogger("nelix.registry")


@dataclass(frozen=True)
class GenerationHandle:
    generation_id: str
    epoch: str
    transport: object
    build_id: "str | None"
    incarnation: dict


def _incarnation_meta(inc: dict) -> str:
    return json.dumps(inc, sort_keys=True)


class GenerationRegistry:

    def __init__(self, *, store=None, build_id=None,
                 supervisor=None, health_probe=None):
        self._store = store
        self._lock = threading.Lock()
        self._gen_locks = {}
        self._gen_locks_lock = threading.Lock()
        self._build_id = build_id
        self._sup = supervisor
        self._generation_id = None
        self._active = None
        self._topology_revision = 1

        # Eager re-adoption in constructor.
        if self._store is not None and self._sup is None:
            self._eager_reconcile()

    # ── eager re-adoption (§3, full case matrix) ──────────────────────────

    def _eager_reconcile(self):
        """Run during construction: reconcile ALL durable generations.
        DB-first: list_generations(), for each construct its GenerationSupervisor,
        inspect the lock holder + strict /health, repair to a consistent state.

        FAIL-CLOSED on store read errors — never mint a second active row.
        """
        if self._store is None:
            return

        # C5: FAIL CLOSED — a store read error must abort, not silently mint.
        try:
            gens = self._store.list_generations()
        except Exception:
            raise NelixError(GENERATION_UNAVAILABLE,
                             "failed to read generation list during re-adoption; "
                             "store may be corrupted")

        # Multiple active rows → FAIL CLOSED
        active_rows = [g for g in gens if g.lifecycle_state == "active"]
        if len(active_rows) > 1:
            raise NelixError(IDEMPOTENCY_CONFLICT,
                             f"multiple ({len(active_rows)}) active generation rows found; "
                             "store is corrupted")

        if not active_rows:
            return  # No active — first call to active() will mint one.

        gen_rec = active_rows[0]
        gid = gen_rec.generation_id
        build_id = gen_rec.build_id

        self._generation_id = gid

        # H9: NEVER reject a pinned OLDER runtime — pinning IS the point.
        # The runtime may not match ours, and that's fine; the generation
        # runs its OWN pinned code. Only fail if build_id is None AND
        # our runtime is also None (both checkout — in dev that's ok too).

        sup = GenerationSupervisor(gid, build_id)
        current_epoch = gen_rec.current_epoch

        # C5: FAIL CLOSED — a store read error on epochs must abort.
        try:
            all_epochs = self._store.list_epochs_strict(gid)
        except Exception:
            raise NelixError(GENERATION_UNAVAILABLE,
                             "failed to read epoch list during re-adoption; "
                             "store may be corrupted")

        # C6: Reap dirs with no DB row.
        # TODO(S1c-2 hardening bead): dir-only orphan reaping in empty/no-active case
        try:
            for d in sup.generation_dir().parent.iterdir():
                if not d.is_dir():
                    continue
                dir_name = d.name
                if dir_name not in {g.generation_id for g in gens}:
                    _log.warning("registry: orphan generation dir %s (no DB row); reaping", dir_name)
                    # Best-effort: reap the lock holder if any.
                    try:
                        os_sup = GenerationSupervisor(dir_name, None)
                        holder = os_sup._live_lock_holder()
                        if holder:
                            inc = {"pid": holder["pid"],
                                   "start_fingerprint": holder.get("start_fingerprint")}
                            os_sup.reap_holder(inc)
                    except Exception:
                        pass
        except OSError:
            pass

        # C6: current_epoch=NULL but epochs exist → repair to a consistent state.
        if current_epoch is None:
            serving = [e for e in all_epochs if e.process_state == "serving"]
            starting = [e for e in all_epochs if e.process_state == "starting"]

            # Try to adopt a serving epoch with a live matching holder.
            # Repair the dangling current_epoch pointer rather than dead-reconciling.
            for se in serving:
                holder = sup._live_lock_holder()
                if holder is None:
                    continue
                transport = sup._holder_transport(holder)
                if transport is None:
                    # TCP/unknown holder — try endpoint.
                    ep = sup.endpoint(expected_epoch=se.generation_epoch)
                    if ep is None:
                        continue
                    transport = ep
                identity = sup._health_identity(transport)
                if identity is None:
                    continue
                if (identity.get("generation_id") == gid
                        and identity.get("generation_epoch") == se.generation_epoch
                        and identity.get("build_id") == build_id):
                    # C1: Re-read holder before installing.
                    re_read = sup._live_lock_holder()
                    if (not re_read
                            or re_read.get("pid") != holder["pid"]
                            or re_read.get("start_fingerprint") != holder.get("start_fingerprint")):
                        _log.warning("registry: holder changed during NULL repair; aborting")
                        return
                    inc = {"pid": holder["pid"],
                           "start_fingerprint": holder.get("start_fingerprint")}
                    _log.info("registry: repairing NULL current_epoch -> %s (serving, live)",
                              se.generation_epoch)
                    # C4: DIRECT repair of the dangling pointer.
                    # Must NOT swallow the failure — a NULL pointer with a live
                    # daemon is an inconsistency; don't return an active handle.
                    try:
                        self._store.set_current_epoch(gid, se.generation_epoch)
                    except NelixError:
                        _log.error("registry: failed to repair NULL current_epoch -> %s; "
                                   "aborting adoption", se.generation_epoch)
                        raise
                    self._active = {
                        "incarnation": inc, "epoch": se.generation_epoch,
                        "generation_id": gid, "transport": transport,
                        "build_id": build_id,
                    }
                    # Don't return yet — also reconcile remaining starting epochs below.
                    break

            # If we installed _active from a serving epoch, still reconcile
            # remaining starting epochs.
            for se in starting:
                holder = sup._live_lock_holder()
                if holder is None:
                    continue
                transport = sup._holder_transport(holder)
                if transport is None:
                    # TCP/unknown holder — try endpoint.
                    ep = sup.endpoint(expected_epoch=se.generation_epoch)
                    if ep is None:
                        continue
                    transport = ep
                identity = sup._health_identity(transport)
                if identity is None:
                    continue
                if (identity.get("generation_id") == gid
                        and identity.get("generation_epoch") == se.generation_epoch
                        and identity.get("build_id") == build_id):
                    # C1: Re-read holder BEFORE CAS — incarnation must not have changed.
                    re_read = sup._live_lock_holder()
                    if (not re_read
                            or re_read.get("pid") != holder["pid"]
                            or re_read.get("start_fingerprint") != holder.get("start_fingerprint")):
                        _log.warning("registry: holder changed before NULL-starting eager CAS; "
                                     "aborting promotion without reaping replacement")
                        return
                    inc = {"pid": holder["pid"],
                           "start_fingerprint": holder.get("start_fingerprint")}
                    _log.info("registry: repairing NULL current_epoch → promoting starting %s",
                              se.generation_epoch)
                    try:
                        self._store.cas_epoch_serving(
                            gid, se.generation_epoch,
                            expected_current_epoch=None,
                            incarnation_meta=_incarnation_meta(inc))
                    except NelixError:
                        self._store.reconcile_epoch_dead(gid, se.generation_epoch)
                        continue
                    self._active = {
                        "incarnation": inc, "epoch": se.generation_epoch,
                        "generation_id": gid, "transport": transport,
                        "build_id": build_id,
                    }
                    break

            # No live matching holder OR _active already set —
            # reconcile all remaining starting/serving to dead.
            if self._active is not None:
                # We already adopted a serving/starting epoch above.
                # Just clean up any remaining starting epochs — EXCLUDE the
                # one we just promoted (it's now serving, not dead).
                adopted_epoch = self._active.get("epoch")
                for se in starting:
                    if adopted_epoch is not None and se.generation_epoch == adopted_epoch:
                        continue
                    try:
                        self._store.reconcile_epoch_dead(gid, se.generation_epoch)
                    except NelixError:
                        pass
                return
            for se in serving + starting:
                try:
                    self._store.reconcile_epoch_dead(gid, se.generation_epoch)
                except NelixError:
                    pass
            return

        # Inspect the current_epoch's holder.
        # TODO(S1c-2 hardening bead): bounded not-yet-healthy startup stabilization
        holder = sup._live_lock_holder()
        epoch_recs = {e.generation_epoch: e for e in all_epochs}
        epoch_rec = epoch_recs.get(current_epoch)

        if holder is not None:
            transport = sup._holder_transport(holder)

            if transport is not None:
                # C3: STRICT health check (key presence, not .get()).
                identity = sup._health_identity(transport) if sup._check_health(transport) else None
                if identity is not None and all(k in identity for k in ("generation_id", "generation_epoch", "build_id")):
                    full_match = (
                        identity["generation_id"] == gid
                        and identity["generation_epoch"] == current_epoch
                        and identity["build_id"] == build_id
                    )
                    inc = {"pid": holder["pid"],
                           "start_fingerprint": holder.get("start_fingerprint")}

                    if full_match and epoch_rec and epoch_rec.process_state == "serving":
                        # C1: Re-read holder before install — incarnation must not have changed.
                        # If it changed, ABORT without reaping (the new holder may be legitimate).
                        re_read = sup._live_lock_holder()
                        if (not re_read
                                or re_read.get("pid") != holder["pid"]
                                or re_read.get("start_fingerprint") != holder.get("start_fingerprint")):
                            _log.warning("registry: holder changed during eager health probe; "
                                         "aborting adoption without reaping replacement")
                            return
                        _log.info("registry: re-adopting serving epoch %s", current_epoch)
                        self._active = {
                            "incarnation": inc, "epoch": current_epoch,
                            "generation_id": gid, "transport": transport,
                            "build_id": build_id,
                        }
                        return

                    if full_match and epoch_rec and epoch_rec.process_state == "starting":
                        # C1: Re-read holder BEFORE CAS — incarnation must not have changed.
                        re_read = sup._live_lock_holder()
                        if (not re_read
                                or re_read.get("pid") != holder["pid"]
                                or re_read.get("start_fingerprint") != holder.get("start_fingerprint")):
                            _log.warning("registry: holder changed before eager CAS; "
                                         "aborting promotion without reaping replacement")
                            return
                        _log.info("registry: promoting starting epoch %s", current_epoch)
                        if self._store:
                            try:
                                self._store.cas_epoch_serving(gid, current_epoch,
                                                              expected_current_epoch=current_epoch,
                                                              incarnation_meta=_incarnation_meta(inc))
                            except NelixError:
                                self._store.reconcile_epoch_dead(gid, current_epoch)
                                return
                        # C1: Re-read holder after CAS — incarnation must not have changed.
                        re_read2 = sup._live_lock_holder()
                        if (not re_read2
                                or re_read2.get("pid") != holder["pid"]
                                or re_read2.get("start_fingerprint") != holder.get("start_fingerprint")):
                            _log.warning("registry: holder changed during eager CAS; "
                                         "aborting without reaping replacement")
                            self._store.reconcile_epoch_dead(gid, current_epoch)
                            return
                        self._active = {
                            "incarnation": inc, "epoch": current_epoch,
                            "generation_id": gid, "transport": transport,
                            "build_id": build_id,
                        }
                        return

                # Identity mismatch — don't reap (holder may be a legitimate
                # replacement); just reconcile the epoch dead.
                # The reap+spawn cycle belongs to the active() mint path, not
                # the eager reconcile constructor.

            else:
                # C2: TCP holder — route through endpoint() with expected_epoch
                # verification, not inline health checks.
                inc = {"pid": holder["pid"],
                       "start_fingerprint": holder.get("start_fingerprint")}
                ep = sup.endpoint(expected_epoch=current_epoch)
                if ep is not None:
                    # C1: Re-read holder before install — abort if changed, don't reap.
                    re_read = sup._live_lock_holder()
                    if (not re_read
                            or re_read.get("pid") != holder["pid"]
                            or re_read.get("start_fingerprint") != holder.get("start_fingerprint")):
                        _log.warning("registry: TCP holder changed during eager health; "
                                     "aborting adoption without reaping replacement")
                        return
                    self._active = {
                        "incarnation": inc, "epoch": current_epoch,
                        "generation_id": gid, "transport": ep,
                        "build_id": build_id,
                    }
                    return
                # Don't reap — the holder may be a legitimate replacement.
                # Just reconcile the epoch dead below.

        # C6: Handle ALL historical starting epochs.
        # Reconcile the current_epoch (serving/starting with no match).
        if current_epoch:
            try:
                self._store.reconcile_epoch_dead(gid, current_epoch)
            except NelixError as e:
                _log.warning("registry: reconcile_epoch_dead(%s, %s) failed: %s",
                             gid, current_epoch, e)
                # Surface the error — a swallowed reconcile may leave the DB in a state
                # where a subsequent fresh-epoch CAS cannot succeed.
                raise

        # C6: Every OTHER starting epoch → dead.
        # TODO(S1c-2 hardening bead): multiple historical starting epochs reconciliation
        for e in all_epochs:
            if e.generation_epoch == current_epoch:
                continue
            if e.process_state == "starting":
                try:
                    self._store.reconcile_epoch_dead(gid, e.generation_epoch)
                except NelixError as e:
                    _log.warning("registry: reconcile_epoch_dead(%s, %s) failed: %s",
                                 gid, e.generation_epoch, e)
                    raise

    def _get_epoch(self, gid, epoch):
        if self._store is None:
            return None
        try:
            for ep in self._store.list_epochs(gid):
                if ep.generation_epoch == epoch:
                    return ep
        except Exception:
            pass
        return None

    # ── per-generation single-flight ──────────────────────────────────────

    def _gen_lock(self, gid: str) -> threading.Lock:
        with self._gen_locks_lock:
            if gid not in self._gen_locks:
                self._gen_locks[gid] = threading.Lock()
            return self._gen_locks[gid]

    # ── active() ──────────────────────────────────────────────────────────

    def active(self) -> GenerationHandle:
        if self._sup is not None:
            return self._active_compat()

        with self._lock:
            if self._active is not None:
                return self._refresh_active_locked()
            return self._find_or_create_locked()

    def _refresh_active_locked(self) -> GenerationHandle:
        """C1+C2: Re-verify full identity + serving state + durable pointer,
        not just /status."""
        a = self._active
        gid = a["generation_id"]
        epoch = a["epoch"]
        build_id = a.get("build_id")
        sup = GenerationSupervisor(gid, build_id)

        holder = sup._live_lock_holder()
        if holder is None:
            raise NelixError(GENERATION_UNAVAILABLE,
                             f"generation {gid} has no live lock holder")

        # #4: For TCP holders, _holder_transport returns None.  Use endpoint()
        # (which reads the state file and constructs the tokened transport) instead.
        transport = sup._holder_transport(holder)
        if transport is None:
            # TCP or otherwise unreachable via holder meta — try endpoint.
            ep = sup.endpoint(expected_epoch=epoch)
            if ep is None:
                raise NelixError(GENERATION_UNAVAILABLE,
                                 f"generation {gid} has unreachable holder")
            transport = ep

        # C2: Verify FULL identity triple via STRICT health check.
        if not sup._check_health_strict(transport, epoch, gid, build_id):
            raise NelixError(GENERATION_UNAVAILABLE,
                             f"generation {gid} identity mismatch on refresh")

        # C1: Compare fingerprint, not just PID.
        current = {"pid": holder["pid"],
                   "start_fingerprint": holder.get("start_fingerprint")}
        prev = a.get("incarnation", {})
        if (current.get("pid") != prev.get("pid")
                or current.get("start_fingerprint") != prev.get("start_fingerprint")):
            raise NelixError(GENERATION_UNAVAILABLE,
                             f"generation {gid} incarnation changed on refresh")

        if self._store:
            gen_rec = self._store.get_generation(gid)
            # C4: Do NOT silently accept a null durable current_epoch.
            # When the pointer is NULL but we have a verified live serving daemon,
            # repair the pointer. Otherwise fail — null pointer means inconsistency.
            if gen_rec.current_epoch is None:
                # Check the epoch row: it must be 'serving' for us to trust it.
                epoch_list = self._store.list_epochs_strict(gid)
                epoch_found = any(e.generation_epoch == epoch and e.process_state == "serving"
                                  for e in epoch_list)
                if epoch_found:
                    # C4: DIRECT repair — set current_epoch to the serving epoch.
                    # Do NOT use cas_epoch_serving (which requires 'starting' state).
                    # Must NOT swallow the failure — don't return an active handle
                    # while the durable pointer is still NULL.
                    _log.warning("registry: repairing NULL current_epoch -> %s", epoch)
                    try:
                        self._store.set_current_epoch(gid, epoch)
                    except NelixError:
                        _log.error("registry: failed to repair NULL current_epoch -> %s; "
                                   "aborting", epoch)
                        raise
                else:
                    raise NelixError(GENERATION_UNAVAILABLE,
                                     f"generation {gid} current_epoch is NULL and "
                                     f"epoch {epoch} is not serving")
            elif gen_rec.current_epoch != epoch:
                # C4: Non-serving pointer — check if the pointed epoch is still serving.
                # If it's dead/starting, reject.
                epoch_list = self._store.list_epochs_strict(gid)
                pointed = [e for e in epoch_list
                           if e.generation_epoch == gen_rec.current_epoch]
                if pointed and pointed[0].process_state != "serving":
                    raise NelixError(GENERATION_UNAVAILABLE,
                                     f"generation {gid} current_epoch "
                                     f"{gen_rec.current_epoch!r} is not serving "
                                     f"(actual: {pointed[0].process_state})")
                raise NelixError(GENERATION_UNAVAILABLE,
                                 f"generation {gid} current_epoch changed to "
                                 f"{gen_rec.current_epoch!r}, expected {epoch!r}")
            else:
                # C4: current_epoch == epoch — verify the epoch row is 'serving'.
                # A dead/starting/missing row with a matching pointer must NOT
                # be routed; the pointer is stale.
                epoch_list = self._store.list_epochs_strict(gid)
                matched = [e for e in epoch_list
                           if e.generation_epoch == epoch]
                if not matched:
                    raise NelixError(GENERATION_UNAVAILABLE,
                                     f"generation {gid} current_epoch {epoch!r} "
                                     f"has no matching epoch row")
                if matched[0].process_state != "serving":
                    raise NelixError(GENERATION_UNAVAILABLE,
                                     f"generation {gid} current_epoch {epoch!r} "
                                     f"is not serving (actual: {matched[0].process_state})")

        # TOCTOU: Re-read the holder after all health/store checks and
        # before RETURNING the handle. If the holder changed (A→B), do not
        # return A's identity with a transport now reaching B.
        re_read = sup._live_lock_holder()
        if (not re_read
                or re_read.get("pid") != current.get("pid")
                or re_read.get("start_fingerprint") != current.get("start_fingerprint")):
            raise NelixError(GENERATION_UNAVAILABLE,
                             f"generation {gid} holder changed during refresh; "
                             f"aborting without adopting replacement")

        self._active["transport"] = transport
        self._active["incarnation"] = current
        return GenerationHandle(generation_id=gid, epoch=epoch,
                                transport=transport, build_id=build_id,
                                incarnation=current)

    def _find_or_create_locked(self) -> GenerationHandle:
        """C5: FAIL CLOSED on store read errors, never silently mint."""
        gid = None
        build_id = self._build_id

        if self._store is not None:
            try:
                gens = self._store.list_generations()
            except Exception:
                raise NelixError(GENERATION_UNAVAILABLE,
                                 "failed to read generation list; store may be corrupted")

            active_rows = [g for g in gens if g.lifecycle_state == "active"]
            if len(active_rows) > 1:
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 "multiple active generation rows found")
            if active_rows:
                gr = active_rows[0]
                gid = gr.generation_id
                build_id = gr.build_id  # H9: use the PINNED build, never override

        if gid is None:
            if build_id is None:
                # C8: ONLY ModuleNotFoundError(name='runtime') means "no runtime"
                # (dev checkout). A broken module / missing 'active' export MUST
                # PROPAGATE — fail closed, never silently use None.
                try:
                    from runtime import active
                except ModuleNotFoundError as e:
                    if e.name != "runtime":
                        raise
                else:
                    build_id = active()  # let RuntimeError propagate — fail closed
            if build_id is None:
                _log.warning("registry: bootstrapping with build_id=None (checkout code); "
                             "provision a runtime for production")
            gid = new_generation_id()

        self._generation_id = gid
        return self._single_flight_mint_and_install(gid, build_id)

    def _single_flight_mint_and_install(self, gid: str,
                                         build_id: "str | None") -> GenerationHandle:
        gen_lock = self._gen_lock(gid)
        with gen_lock:
            return self._mint_install_unlocked(gid, build_id)

    def _mint_install_unlocked(self, gid: str,
                                build_id: "str | None") -> GenerationHandle:
        clock = time.time()

        if self._store is not None:
            try:
                self._store.create_generation(gid, build_id=build_id,
                                              lifecycle_state="active",
                                              capability_snapshot=None, created_at=clock)
            except NelixError as e:
                if e.code != "duplicate_start":
                    raise NelixError(GENERATION_UNAVAILABLE,
                                     f"failed to persist generation: {e}") from None

        epoch = new_generation_id()
        if self._store is not None:
            try:
                self._store.insert_epoch(epoch, gid, incarnation_meta=None, created_at=clock)
            except NelixError as e:
                raise NelixError(GENERATION_UNAVAILABLE,
                                 f"failed to persist starting epoch: {e}") from None

        sup = GenerationSupervisor(gid, build_id)
        incarnation = None
        transport = None
        try:
            incarnation, transport = sup.ensure_running(epoch)
        except Exception as e:
            if self._store is not None:
                try:
                    self._store.reconcile_epoch_dead(gid, epoch)
                except Exception:
                    pass
            raise NelixError(GENERATION_UNAVAILABLE,
                             f"failed to spawn generation daemon: {e}") from None

        # 4. STRICT /health check.
        if not sup._check_health_strict(transport, epoch, gid, build_id):
            if incarnation:
                sup.reap_holder(incarnation)
            if self._store:
                try:
                    self._store.reconcile_epoch_dead(gid, epoch)
                except Exception:
                    pass
            raise NelixError(GENERATION_UNAVAILABLE,
                             "generation daemon health check failed (strict identity mismatch)")

        # 5. C1: Re-read holder, compare FINGERPRINT, not just PID.
        holder = sup._live_lock_holder()
        if not holder:
            if incarnation:
                sup.reap_holder(incarnation)
            if self._store:
                try:
                    self._store.reconcile_epoch_dead(gid, epoch)
                except Exception:
                    pass
            raise NelixError(GENERATION_UNAVAILABLE,
                             "generation daemon vanished before installation")

        # C1: Compare both pid AND fingerprint.
        if (holder["pid"] != incarnation["pid"]
                or holder.get("start_fingerprint") != incarnation.get("start_fingerprint")):
            _log.warning("registry: holder replaced during health check gen_id=%s "
                         "expected=(pid=%s fp=%s) actual=(pid=%s fp=%s)",
                         gid, incarnation["pid"], incarnation.get("start_fingerprint"),
                         holder["pid"], holder.get("start_fingerprint"))
            raise NelixError(GENERATION_UNAVAILABLE,
                             "generation daemon holder replaced during health check")

        current_incarnation = {"pid": holder["pid"],
                               "start_fingerprint": holder.get("start_fingerprint")}

        # 6. C7: Guarded CAS with incarnation recording.
        if self._store is not None:
            try:
                self._store.cas_epoch_serving(gid, epoch, expected_current_epoch=None,
                                              incarnation_meta=_incarnation_meta(current_incarnation))
            except NelixError:
                sup.reap_holder(current_incarnation)
                try:
                    self._store.reconcile_epoch_dead(gid, epoch)
                except Exception:
                    pass
                raise NelixError(GENERATION_UNAVAILABLE,
                                 "epoch promotion failed (CAS conflict)")

        # 7. C1: Final validation — compare FINGERPRINT.
        holder = sup._live_lock_holder()
        if (not holder
                or holder.get("pid") != current_incarnation["pid"]
                or holder.get("start_fingerprint") != current_incarnation.get("start_fingerprint")):
            if self._store:
                try:
                    self._store.reconcile_epoch_dead(gid, epoch)
                except Exception:
                    pass
            raise NelixError(GENERATION_UNAVAILABLE,
                             "generation daemon replaced after promotion")

        self._active = {
            "incarnation": current_incarnation, "epoch": epoch,
            "generation_id": gid, "transport": transport, "build_id": build_id,
        }
        self._bump_topology_locked()
        return GenerationHandle(generation_id=gid, epoch=epoch, transport=transport,
                                build_id=build_id, incarnation=current_incarnation)

    # ── generation resolution by (id, epoch) ──────────────────────────────

    def resolve_generation_state(self, generation_id: str, generation_epoch: str):
        """Resolve a generation by (id, epoch), returning state info.

        Returns (process_state, lifecycle_state, capability_snapshot, handle_or_None).
        When process_state is 'serving' and the generation matches the active one,
        a GenerationHandle is returned for forwarding.
        """
        if self._store is None:
            return None, None, None, None

        gen_rec = self._store.get_generation(generation_id)
        epochs = self._store.list_epochs(generation_id)
        epoch_rec = next((e for e in epochs if e.generation_epoch == generation_epoch), None)
        if epoch_rec is None:
            raise NelixError(UNKNOWN_SESSION,
                             f"epoch {generation_epoch} not found for generation {generation_id}")

        proc_state = epoch_rec.process_state
        lc_state = gen_rec.lifecycle_state
        cap_snap = gen_rec.capability_snapshot

        handle = None
        if proc_state == "serving":
            with self._lock:
                if self._active is not None:
                    a = self._active
                    if a["generation_id"] == generation_id and a["epoch"] == generation_epoch:
                        try:
                            handle = self._refresh_active_locked()
                        except NelixError:
                            pass
        return proc_state, lc_state, cap_snap, handle

    # ── topology revision ────────────────────────────────────────────────

    def _bump_topology_locked(self):
        self._topology_revision += 1

    # ── compat path (test mock) ───────────────────────────────────────────

    def _active_compat(self) -> GenerationHandle:
        self._ensure_available_compat()
        with self._lock:
            snap = self._sup.held_generation()
            if snap is None:
                raise NelixError(GENERATION_UNAVAILABLE,
                                 "generation disappeared before it could be pinned")
            transport, inc = snap
            if self._active is None:
                return self._first_observation_compat(transport, inc)
            if self._active["incarnation"] != inc:
                return self._new_incarnation_compat(transport, inc)
            self._active["transport"] = transport
            epoch = self._active["epoch"]
            gid = self._active["generation_id"]
            build_id = self._active.get("build_id")
            return GenerationHandle(generation_id=gid, epoch=epoch,
                                    transport=transport, build_id=build_id, incarnation=inc)

    def _ensure_available_compat(self):
        try:
            if self._sup.active_generation() is None:
                self._sup.ensure_running()
                if self._sup.active_generation() is None:
                    raise NelixError(GENERATION_UNAVAILABLE, "no generation backend available")
        except NelixError:
            raise
        except Exception as e:
            raise NelixError(GENERATION_UNAVAILABLE,
                             f"could not make a generation available: {e}") from None

    def _first_observation_compat(self, transport, inc):
        clock = time.time()
        gid = self._generation_id or new_generation_id()
        self._generation_id = gid
        build_id = self._build_id
        if self._store:
            try:
                self._store.create_generation(gid, build_id=build_id, lifecycle_state="active",
                                              capability_snapshot=None, created_at=clock)
            except Exception:
                raise NelixError(GENERATION_UNAVAILABLE, "failed to persist the generation identity")
        epoch = new_generation_id()
        if self._store:
            try:
                self._store.insert_epoch(epoch, gid, incarnation_meta=_incarnation_meta(inc),
                                         created_at=clock)
                self._store.cas_epoch_serving(gid, epoch, expected_current_epoch=None,
                                              incarnation_meta=_incarnation_meta(inc))
            except Exception:
                self._store.set_epoch_process_state(epoch, "dead")
                raise NelixError(GENERATION_UNAVAILABLE, "epoch promotion failed")
        self._active = {"incarnation": inc, "epoch": epoch, "generation_id": gid,
                        "transport": transport, "build_id": build_id}
        return GenerationHandle(generation_id=gid, epoch=epoch, transport=transport,
                                build_id=build_id, incarnation=inc)

    def _new_incarnation_compat(self, transport, inc):
        gid = self._active["generation_id"]
        old_epoch = self._active["epoch"]
        if self._store:
            try:
                self._store.set_epoch_process_state(old_epoch, "dead")
            except Exception:
                pass
        clock = time.time()
        epoch = new_generation_id()
        if self._store:
            try:
                self._store.insert_epoch(epoch, gid, incarnation_meta=_incarnation_meta(inc),
                                         created_at=clock)
                self._store.cas_epoch_serving(gid, epoch, expected_current_epoch=old_epoch,
                                              incarnation_meta=_incarnation_meta(inc))
            except Exception:
                self._store.set_epoch_process_state(epoch, "dead")
                raise NelixError(GENERATION_UNAVAILABLE, "epoch promotion failed")
        build_id = self._build_id or self._active.get("build_id")
        self._active = {"incarnation": inc, "epoch": epoch, "generation_id": gid,
                        "transport": transport, "build_id": build_id}
        return GenerationHandle(generation_id=gid, epoch=epoch, transport=transport,
                                build_id=build_id, incarnation=inc)

    # ── topology / generations / discovery ────────────────────────────────

    def adopt_generation(self, generation_id: str, epoch: str, transport,
                          build_id: "str | None" = None,
                          incarnation: "dict | None" = None) -> GenerationHandle:
        """Adopt a newly-activated generation into the registry's in-memory state.

        Called by the operator path AFTER the atomic store flip has committed
        (old->draining + new->active). Sets _active, bumps topology_revision, and
        returns a GenerationHandle. Only call this AFTER the store transaction
        has committed — never on health-check failure.

        When incarnation is None, reads it from the live lock holder (the normal
        operator path). Tests may provide it directly.
        """
        if incarnation is None:
            sup = GenerationSupervisor(generation_id, build_id)
            holder = sup._live_lock_holder()
            if not holder:
                raise NelixError(GENERATION_UNAVAILABLE,
                                 f"generation {generation_id} has no live lock holder")
            incarnation = {"pid": holder["pid"],
                           "start_fingerprint": holder.get("start_fingerprint")}
        with self._lock:
            self._generation_id = generation_id
            self._active = {
                "incarnation": incarnation, "epoch": epoch,
                "generation_id": generation_id, "transport": transport,
                "build_id": build_id,
            }
            self._bump_topology_locked()
        return GenerationHandle(generation_id=generation_id, epoch=epoch,
                                transport=transport, build_id=build_id,
                                incarnation=incarnation)

    def topology_revision(self) -> int:
        with self._lock:
            return self._topology_revision

    def generations(self, *, discover=False) -> list:
        with self._lock:
            if self._active is None and discover:
                self._discover_locked()
            return [] if self._active is None else [self._handle(self._active)]

    def _discover_locked(self):
        """C4: Non-spawning probe. If a daemon holds the lock, create a starting
        epoch AND attempt guarded promotion — never install _active without promotion."""
        if self._active is not None:
            return

        if self._sup is not None:
            snap = self._sup.held_generation()
            if snap is None:
                return
            transport, inc = snap
            gid = self._generation_id or new_generation_id()
            self._generation_id = gid
            clock = time.time()
            if self._store:
                try:
                    self._store.create_generation(gid, build_id=None, lifecycle_state="active",
                                                  capability_snapshot=None, created_at=clock)
                except NelixError:
                    return
            epoch = new_generation_id()
            if self._store:
                try:
                    self._store.insert_epoch(epoch, gid,
                                             incarnation_meta=_incarnation_meta(inc),
                                             created_at=clock)
                    self._store.cas_epoch_serving(gid, epoch, expected_current_epoch=None,
                                                  incarnation_meta=_incarnation_meta(inc))
                except NelixError:
                    return
            self._active = {"incarnation": inc, "epoch": epoch, "generation_id": gid,
                            "transport": transport, "build_id": None}
            return

        gid = self._generation_id
        if gid is None:
            if self._store:
                try:
                    gens = self._store.list_generations()
                    active_rows = [g for g in gens if g.lifecycle_state == "active"]
                    if active_rows:
                        gid = active_rows[0].generation_id
                        self._generation_id = gid
                except Exception:
                    pass
        if gid is None:
            return

        sup = GenerationSupervisor(gid, self._build_id)
        holder = sup._live_lock_holder()
        if holder is None:
            return
        transport = sup._holder_transport(holder)
        if transport is None:
            return

        # C2+C3: Strict identity — verify FULL triple with key presence.
        identity = sup._health_identity(transport)
        if identity is None:
            return
        # _health_identity already enforces key presence (C3).
        if (identity["generation_id"] != gid
                or identity["build_id"] != self._build_id):
            return

        incarnation = {"pid": holder["pid"],
                       "start_fingerprint": holder.get("start_fingerprint")}
        # C4: Adopt the daemon's REPORTED epoch, not invent a new one.
        reported_epoch = identity.get("generation_epoch")
        if not reported_epoch:
            return  # daemon has no epoch — cannot adopt
        clock = time.time()

        if self._store:
            try:
                self._store.create_generation(gid, build_id=self._build_id,
                                              lifecycle_state="active",
                                              capability_snapshot=None, created_at=clock)
            except NelixError:
                pass
            try:
                self._store.insert_epoch(reported_epoch, gid,
                                         incarnation_meta=_incarnation_meta(incarnation),
                                         created_at=clock)
            except NelixError:
                return
            # C4: Attempt guarded promotion using the REPORTED epoch.
            try:
                self._store.cas_epoch_serving(gid, reported_epoch, expected_current_epoch=None,
                                              incarnation_meta=_incarnation_meta(incarnation))
            except NelixError:
                try:
                    self._store.reconcile_epoch_dead(gid, reported_epoch)
                except Exception:
                    pass
                return

        self._active = {
            "incarnation": incarnation, "epoch": reported_epoch,
            "generation_id": gid, "transport": transport, "build_id": self._build_id,
        }

    def _handle(self, a) -> GenerationHandle:
        return GenerationHandle(generation_id=a["generation_id"], epoch=a["epoch"],
                                transport=a["transport"], build_id=a["build_id"],
                                incarnation=a["incarnation"])
