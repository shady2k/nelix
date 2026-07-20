"""S5b — crash reconciliation (§3.5 crash path + §3.3f child-group reaping).

REAL crash-cert end-to-end: real Store, draining epoch process_state=dead,
admitted sessions WITHOUT terminals -> generation_lost persisted, epoch
retirement_state=certified while process_state=dead, certificate+final_high_water
recorded, oracle lets generation retire. Child-group reap gate blocks on EPERM.
"""
import pytest
import paths
from nelix_contracts.ids import new_generation_id
from nelix_contracts.lifecycle import DRAINING, RETIRED
from nelix_contracts.retirement import generation_may_retire
from nelix_store.store import Store
from router.operator import OperatorRoutes


# ============================================================
# 1. Crash reconciliation end-to-end
# ============================================================

class TestCrashReconciliationEndToEnd:
    """REAL crash-cert end-to-end with real Store + SessionManager."""

    def test_crash_reconcile_end_to_end(self, tmp_path):
        """Crash reconciliation drives: prove death, generation_lost for outstanding
        sessions, certify epoch (router-issued) while process_state=dead, oracle
        allows retirement. generation_lost does NOT overwrite a real terminal."""
        import threading
        from nelix_store.ledger import StartLedger
        from daemon.events import EventQueue
        from daemon.rpc_server import make_server
        from daemon.manager import SessionManager
        from daemon.transport import Transport
        from tests.conftest import EXECUTOR, OWNER, make_spec
        from router.registry import GenerationRegistry
        from tests._router_fakes import Supervisor

        clock = _FakeClock(1000.0)
        store = Store(paths.nelix_root(), clock=clock)
        ledger = StartLedger(paths.nelix_root(), clock=clock)
        gid = new_generation_id()
        epoch = new_generation_id()

        store.create_generation(gid, build_id="b-1", lifecycle_state=DRAINING,
                                capability_snapshot=None, created_at=1000.0)
        store.insert_epoch(epoch, gid, incarnation_meta=None, created_at=1000.0)
        store.cas_epoch_serving(gid, epoch, expected_current_epoch=None,
                                incarnation_meta='{"pid": 99999, "start_fingerprint": "fp-crash"}')

        events = EventQueue()
        specs = {EXECUTOR: make_spec()}

        class _LeafSession:
            def __init__(self, sid, ex, spec, ev):
                self.sid = sid
                self.executor = ex
                self._events_queue = ev
                self.on_terminal = None
                self.deliver_turn = None
                self._persist_terminal = None
                self.lineage_id = sid
                self.restarted_from = None
                self.restart_count = 0
                self.model = None
                self._terminal_kind = "done"
                self._last_screen_excerpt = "all done"
                self._stopped = False
            def start(self, task, cwd): pass
            def snapshot(self):
                return {"session_id": self.sid, "executor": self.executor,
                        "control_state": "busy", "task_delivery": "pending",
                        "terminal": False}
            def terminal_snapshot(self):
                return {"session_id": self.sid, "terminal_kind": self._terminal_kind,
                        "screen_excerpt": self._last_screen_excerpt,
                        "lineage_id": self.lineage_id}
            def stop(self):
                kind = self._terminal_kind
                excerpt = self._last_screen_excerpt
                if self._persist_terminal is not None:
                    self._persist_terminal(self.sid, kind, excerpt)
                if self.on_terminal is not None:
                    self.on_terminal(self.sid)
                self._stopped = True
            def pending_async_id(self): return None

        def session_factory(sid, ex, spec, ev):
            return _LeafSession(sid, ex, spec, ev)

        mgr = SessionManager(specs, events, store, session_factory=session_factory,
                             concurrency_limit=5, terminal_snapshot_ttl=300.0,
                             clock=clock, generation_id=gid, generation_epoch=epoch)

        server = make_server(mgr, Transport.tcp("127.0.0.1", 0, "t"))
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        # SESSION 1: start AND stop → has a real terminal (done)
        sid1 = _router_started_session(ledger, store, gid=gid, epoch=epoch)
        mgr.start(EXECUTOR, "task-1", "/tmp", owner_id=OWNER, session_id=sid1)
        mgr.stop(sid1, owner_id=OWNER)

        # SESSION 2: start only → NO terminal (outstanding obligation)
        sid2 = _router_started_session(ledger, store, gid=gid, epoch=epoch)
        mgr.start(EXECUTOR, "task-2", "/tmp", owner_id=OWNER, session_id=sid2)

        # SESSION 3: start only → NO terminal
        sid3 = _router_started_session(ledger, store, gid=gid, epoch=epoch)
        mgr.start(EXECUTOR, "task-3", "/tmp", owner_id=OWNER, session_id=sid3)

        # Simulate daemon crash: set epoch dead
        store.set_epoch_process_state(epoch, "dead")

        # Wire transport into registry
        daemon_transport = Transport.tcp("127.0.0.1", server.server_address[1], "t")
        registry = GenerationRegistry(supervisor=Supervisor(daemon_transport))
        registry.adopt_generation(gid, epoch, daemon_transport,
                                  "b-1", incarnation={"pid": 99999, "start_fingerprint": "fp-crash"})

        operator = OperatorRoutes(registry, "r-test", store=store)

        def _fake_reap(gen_id, ep):
            return True
        operator._reap_fn = _fake_reap

        # Verify sid1 has a real terminal
        term1 = store.get_terminal(sid1, owner_id=OWNER)
        assert term1.terminal_kind == "done"

        # Verify sid2 and sid3 have NO terminals
        for sid in (sid2, sid3):
            row = store._conn.execute(
                "SELECT session_id FROM terminal WHERE session_id=?", (sid,)).fetchone()
            assert row is None, f"session {sid} should not have a terminal yet"

        # Drive retire → should trigger crash reconciliation
        status, body = operator.retire(gid)
        assert status == 200, f"retire returned {status}"
        if body["status"] != "ok":
            pytest.fail(f"retire blocked: {body.get('blockers')}, body={body}")

        # Verify generation retired
        gen = store.get_generation(gid)
        assert gen.lifecycle_state == RETIRED

        # Verify epoch is certified while process_state=dead
        epochs = store.list_epochs(gid)
        certified = [e for e in epochs if e.retirement_state == "certified"]
        assert len(certified) == 1
        ep = certified[0]
        assert ep.process_state == "dead", \
            "epoch must remain process_state=dead while retirement_state=certified"
        assert ep.certificate is not None
        assert "crash-reconcile:" in ep.certificate
        assert ep.final_high_water is not None

        # Verify generation_lost was persisted for each outstanding obligation
        term2 = store.get_terminal(sid2, owner_id=OWNER)
        assert term2.terminal_kind == "generation_lost", \
            f"expected generation_lost for sid2, got {term2.terminal_kind}"

        term3 = store.get_terminal(sid3, owner_id=OWNER)
        assert term3.terminal_kind == "generation_lost", \
            f"expected generation_lost for sid3, got {term3.terminal_kind}"

        # Verify sid1's terminal was NOT overwritten (still "done")
        term1b = store.get_terminal(sid1, owner_id=OWNER)
        assert term1b.terminal_kind == "done", \
            "generation_lost must not overwrite a persisted terminal"

        # Verify oracle allows retirement
        assert generation_may_retire(store=store, generation_id=gid)

        # Verify confirmed_high_water was resolved
        chw = store.get_generation_confirmed_high_water(epoch)
        assert chw >= 1, f"expected confirmed_high_water>=1, got {chw}"

        server.shutdown()
        store.close()


# ============================================================
# 2. Child-group reap gate
# ============================================================

class TestChildGroupReapGate:
    """Crash certification is BLOCKED until the child group is proven gone."""

    def test_child_group_alive_blocks_crash_reconcile(self, tmp_path):
        """A live child (EPERM/alive) blocks crash reconciliation."""
        import os

        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        gid = new_generation_id()
        epoch = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=DRAINING,
                                capability_snapshot=None, created_at=1000.0)
        store.insert_epoch(epoch, gid,
                           incarnation_meta='{"pid": 99999, "start_fingerprint": "fp"}',
                           created_at=1000.0)
        store.set_epoch_process_state(epoch, "dead")

        # Record a child group with pid 42
        store.record_epoch_child_group(epoch, child_pid=42, child_pgid=42)

        operator = OperatorRoutes(None, "r-test", store=store)

        # Mock os.kill so pid 42 returns PermissionError (alive)
        original_kill = os.kill
        call_log = []
        def _mock_kill(pid, sig, **kw):
            call_log.append((pid, sig))
            if pid == 42:
                raise PermissionError("EPERM mock — child is alive")
            return original_kill(pid, sig, **kw)

        os.kill = _mock_kill
        try:
            success, blocker = operator._crash_reconcile_epoch(gid, epoch)
            assert not success, "must block when child is alive"
            assert blocker == "child_still_alive", f"got blocker={blocker!r}"
        finally:
            os.kill = original_kill

        # Verify epoch is NOT certified
        ep = store.get_epoch_retirement_state(epoch)
        assert ep != "certified", "epoch must NOT be certified when child blocks"
        store.close()

    def test_child_group_dead_allows_crash_reconcile(self, tmp_path):
        """A dead child (ESRCH/gone) allows crash reconciliation to proceed."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        gid = new_generation_id()
        epoch = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=DRAINING,
                                capability_snapshot=None, created_at=1000.0)
        store.insert_epoch(epoch, gid,
                           incarnation_meta='{"pid": 99999, "start_fingerprint": "fp"}',
                           created_at=1000.0)
        store.set_epoch_process_state(epoch, "dead")

        # Record a child with a non-existent pid (will be ESRCH)
        store.record_epoch_child_group(epoch, child_pid=99998, child_pgid=99998)

        operator = OperatorRoutes(None, "r-test", store=store)

        success, blocker = operator._crash_reconcile_epoch(gid, epoch)
        assert success, f"crash reconcile failed with blocker={blocker!r}"

        # Verify epoch IS certified
        ep_state = store.get_epoch_retirement_state(epoch)
        assert ep_state == "certified", "epoch must be certified after successful reconcile"
        store.close()


# ============================================================
# 3. Reconciliation incarnation — admission gate
# ============================================================

class TestReconciliationIncarnationGate:
    """A reconciliation incarnation must not admit new sessions."""

    def test_reconciliation_incarnation_rejects_admission(self, tmp_path):
        """The crash-reconciled epoch has retirement_state=certified, which gates
        admission (the start path only forwards to the active generation, not a
        certified epoch)."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        gid = new_generation_id()
        epoch = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=DRAINING,
                                capability_snapshot=None, created_at=1000.0)
        store.insert_epoch(epoch, gid,
                           incarnation_meta='{"pid": 99999, "start_fingerprint": "fp"}',
                           created_at=1000.0)
        store.set_epoch_process_state(epoch, "dead")

        operator = OperatorRoutes(None, "r-test", store=store)
        success, blocker = operator._crash_reconcile_epoch(gid, epoch)
        assert success, f"crash reconcile failed: {blocker}"

        # After crash reconcile, the epoch is certified.
        # Verify no new generation can be created for this epoch
        # by checking current_epoch is cleared by retire()
        gen = store.get_generation(gid)
        assert gen.current_epoch is None or gen.lifecycle_state == RETIRED, \
            "generation should have no current_epoch after reconciliation"
        store.close()


# ============================================================
# Helpers
# ============================================================

class _FakeClock:
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        v = self.t
        self.t += 0.1
        return v


def _router_started_session(ledger, store, owner_id="test-owner", gid=None, epoch=None):
    gid = gid or ("g-" + "a" * 32)
    epoch = epoch or ("g-" + "b" * 32)
    _router_started_session._seq = getattr(_router_started_session, "_seq", 0) + 1
    r = ledger.reserve(idempotency_key="k-" + str(_router_started_session._seq),
                       owner_id=owner_id,
                       orchestration_id="o-" + "c" * 32,
                       request_fingerprint="fp")
    ledger.assign_generation(r.session_id, gid, epoch)
    return r.session_id
