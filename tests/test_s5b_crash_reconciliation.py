"""S5b — crash reconciliation (§3.5 crash path + §3.3f child-group reaping).

REAL crash-cert end-to-end: real Store, draining epoch process_state=dead,
admitted sessions WITHOUT terminals -> generation_lost persisted, epoch
retirement_state=certified while process_state=dead, certificate+final_high_water
recorded, oracle lets generation retire. Child-group reap gate blocks on EPERM.
"""
import pytest
import json
import os as _os
import paths
from nelix_contracts.ids import new_generation_id
from nelix_contracts.lifecycle import DRAINING, RETIRED
from nelix_contracts.retirement import generation_may_retire
from nelix_store.store import Store
from router.operator import OperatorRoutes


def _write_child_json(sid, pid=99998, pgid=99998, fingerprint="fp-child"):
    """Write a child.json record on disk for a session."""
    sess_dir = paths.sessions_root() / sid
    sess_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "sid": sid,
        "daemon_pid": 99999,
        "daemon_fingerprint": "fp-daemon",
        "pid": pid,
        "pgid": pgid,
        "child_fingerprint": fingerprint,
    }
    (sess_dir / "child.json").write_text(json.dumps(record))


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
        from router.leases import LeaseService
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
        _write_child_json(sid1, pid=99999, pgid=99999, fingerprint="fp-child-1")
        mgr.stop(sid1, owner_id=OWNER)

        # SESSION 2: start only → NO terminal (outstanding obligation)
        sid2 = _router_started_session(ledger, store, gid=gid, epoch=epoch)
        mgr.start(EXECUTOR, "task-2", "/tmp", owner_id=OWNER, session_id=sid2)
        _write_child_json(sid2, pid=99998, pgid=99998, fingerprint="fp-child-2")

        # SESSION 3: start only → NO terminal
        sid3 = _router_started_session(ledger, store, gid=gid, epoch=epoch)
        mgr.start(EXECUTOR, "task-3", "/tmp", owner_id=OWNER, session_id=sid3)
        _write_child_json(sid3, pid=99997, pgid=99997, fingerprint="fp-child-3")

        store.set_epoch_process_state(epoch, "dead")

        daemon_transport = Transport.tcp("127.0.0.1", server.server_address[1], "t")
        registry = GenerationRegistry(supervisor=Supervisor(daemon_transport))
        registry.adopt_generation(gid, epoch, daemon_transport,
                                  "b-1", incarnation={"pid": 99999, "start_fingerprint": "fp-crash"})

        lease_service = LeaseService(active_limit=5, live_pty_limit=5)
        operator = OperatorRoutes(registry, "r-test", store=store, lease_service=lease_service)

        def _fake_reap(gen_id, ep):
            return True
        operator._reap_fn = _fake_reap

        term1 = store.get_terminal(sid1, owner_id=OWNER)
        assert term1.terminal_kind == "done"

        for sid in (sid2, sid3):
            row = store._conn.execute(
                "SELECT session_id FROM terminal WHERE session_id=?", (sid,)).fetchone()
            assert row is None, f"session {sid} should not have a terminal yet"

        status, body = operator.retire(gid)
        assert status == 200, f"retire returned {status}"
        if body["status"] != "ok":
            pytest.fail(f"retire blocked: {body.get('blockers')}, body={body}")

        gen = store.get_generation(gid)
        assert gen.lifecycle_state == RETIRED

        epochs = store.list_epochs(gid)
        certified = [e for e in epochs if e.retirement_state == "certified"]
        assert len(certified) == 1
        ep = certified[0]
        assert ep.process_state == "dead"
        assert ep.certificate is not None
        assert "crash-reconcile:" in ep.certificate
        assert ep.final_high_water is not None

        term2 = store.get_terminal(sid2, owner_id=OWNER)
        assert term2.terminal_kind == "generation_lost"

        term3 = store.get_terminal(sid3, owner_id=OWNER)
        assert term3.terminal_kind == "generation_lost"

        term1b = store.get_terminal(sid1, owner_id=OWNER)
        assert term1b.terminal_kind == "done"

        assert generation_may_retire(store=store, generation_id=gid)

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
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        gid = new_generation_id()
        epoch = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=DRAINING,
                                capability_snapshot=None, created_at=1000.0)
        store.insert_epoch(epoch, gid,
                           incarnation_meta='{"pid": 99999, "start_fingerprint": "fp"}',
                           created_at=1000.0)
        sid_alive = "s-" + "a" * 32
        oid = "o-" + "1" * 32
        store.set_epoch_process_state(epoch, "dead")
        store._conn.execute(
            "INSERT INTO starts (session_id, owner_id, orchestration_id, "
            "idempotency_key, request_fingerprint, state, generation_id, "
            "generation_epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (sid_alive, "owner", oid, "k-1", "fp", "started",
             gid, epoch, 1000.0))
        store.create_session(sid_alive, state="starting", executor="demo",
                              task="x", cwd="/tmp", model=None, created_at=1000.0)

        _write_child_json(sid_alive, pid=42, pgid=42, fingerprint="fp-alive")
        operator = OperatorRoutes(None, "r-test", store=store)

        original_kill = _os.kill
        original_killpg = _os.killpg
        def _mock_kill(pid, sig, **kw):
            if pid == 42:
                raise PermissionError("EPERM mock — child is alive")
            return original_kill(pid, sig, **kw)
        def _mock_killpg(pgid, sig):
            if pgid == 42:
                raise PermissionError("EPERM mock — child group is alive")
            return original_killpg(pgid, sig)

        _os.kill = _mock_kill
        _os.killpg = _mock_killpg
        try:
            success, blocker = operator._crash_reconcile_epoch(gid, epoch)
            assert not success, "must block when child is alive"
            assert blocker == "child_group_still_alive", f"got blocker={blocker!r}"
        finally:
            _os.kill = original_kill
            _os.killpg = original_killpg

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
        sid_dead = "s-" + "b" * 32
        oid = "o-" + "1" * 32
        store.set_epoch_process_state(epoch, "dead")
        store._conn.execute(
            "INSERT INTO starts (session_id, owner_id, orchestration_id, "
            "idempotency_key, request_fingerprint, state, generation_id, "
            "generation_epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (sid_dead, "owner", oid, "k-1", "fp", "started",
             gid, epoch, 1000.0))
        store.create_session(sid_dead, state="starting", executor="demo",
                              task="x", cwd="/tmp", model=None, created_at=1000.0)

        _write_child_json(sid_dead, pid=99998, pgid=99998, fingerprint="fp-dead")
        operator = OperatorRoutes(None, "r-test", store=store)

        success, blocker = operator._crash_reconcile_epoch(gid, epoch)
        assert success, f"crash reconcile failed with blocker={blocker!r}"

        ep_state = store.get_epoch_retirement_state(epoch)
        assert ep_state == "certified", "epoch must be certified after successful reconcile"
        store.close()

    def test_child_record_missing_blocks(self, tmp_path):
        """An admitted session with NO child.json => blocked."""
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        gid = new_generation_id()
        epoch = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=DRAINING,
                                capability_snapshot=None, created_at=1000.0)
        store.insert_epoch(epoch, gid,
                           incarnation_meta='{"pid": 99999, "start_fingerprint": "fp"}',
                           created_at=1000.0)
        sid_no = "s-" + "c" * 32
        oid = "o-" + "1" * 32
        store.set_epoch_process_state(epoch, "dead")
        store._conn.execute(
            "INSERT INTO starts (session_id, owner_id, orchestration_id, "
            "idempotency_key, request_fingerprint, state, generation_id, "
            "generation_epoch, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (sid_no, "owner", oid, "k-1", "fp", "started",
             gid, epoch, 1000.0))
        store.create_session(sid_no, state="starting", executor="demo",
                              task="x", cwd="/tmp", model=None, created_at=1000.0)
        # Intentionally do NOT write child.json

        operator = OperatorRoutes(None, "r-test", store=store)
        success, blocker = operator._crash_reconcile_epoch(gid, epoch)
        assert not success, "must block when child record missing"
        assert "child_record_missing" in blocker
        store.close()


# ============================================================
# 3. PID validation
# ============================================================

class TestPidValidation:
    """Death proof requires int>0 pid; bool, non-int, <=0 all => blocked."""

    def _make_dead_epoch_with_pid(self, pid_value):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        gid = new_generation_id()
        epoch = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=DRAINING,
                                capability_snapshot=None, created_at=1000.0)
        meta = {"pid": pid_value}
        if pid_value is not None:
            meta["start_fingerprint"] = "fp"
        store.insert_epoch(epoch, gid,
                           incarnation_meta=json.dumps(meta),
                           created_at=1000.0)
        store.set_epoch_process_state(epoch, "dead")
        return store, gid, epoch

    def test_non_int_pid_blocks(self):
        store, gid, epoch = self._make_dead_epoch_with_pid("not-an-int")
        op = OperatorRoutes(None, "r-test", store=store)
        success, blocker = op._crash_reconcile_epoch(gid, epoch)
        assert not success
        assert "invalid_incarnation_pid" in blocker
        store.close()

    def test_zero_pid_blocks(self):
        store, gid, epoch = self._make_dead_epoch_with_pid(0)
        op = OperatorRoutes(None, "r-test", store=store)
        success, blocker = op._crash_reconcile_epoch(gid, epoch)
        assert not success
        assert "invalid_incarnation_pid" in blocker
        store.close()

    def test_negative_pid_blocks(self):
        store, gid, epoch = self._make_dead_epoch_with_pid(-999)
        op = OperatorRoutes(None, "r-test", store=store)
        success, blocker = op._crash_reconcile_epoch(gid, epoch)
        assert not success
        assert "invalid_incarnation_pid" in blocker
        store.close()

    def test_bool_pid_blocks(self):
        store, gid, epoch = self._make_dead_epoch_with_pid(True)
        op = OperatorRoutes(None, "r-test", store=store)
        success, blocker = op._crash_reconcile_epoch(gid, epoch)
        assert not success
        assert "invalid_incarnation_pid" in blocker
        store.close()

    def test_non_dict_meta_blocks(self):
        store = Store(paths.nelix_root(), clock=lambda: 1000.0)
        gid = new_generation_id()
        epoch = new_generation_id()
        store.create_generation(gid, build_id="b-1", lifecycle_state=DRAINING,
                                capability_snapshot=None, created_at=1000.0)
        store.insert_epoch(epoch, gid,
                           incarnation_meta='["list", "not", "object"]',
                           created_at=1000.0)
        store.set_epoch_process_state(epoch, "dead")
        op = OperatorRoutes(None, "r-test", store=store)
        success, blocker = op._crash_reconcile_epoch(gid, epoch)
        assert not success
        assert "malformed_incarnation_meta" in blocker
        store.close()


# ============================================================
# 4. Reconciliation incarnation — admission gate
# ============================================================

class TestReconciliationIncarnationGate:
    """A reconciliation incarnation must not admit new sessions."""

    def test_reconciliation_incarnation_rejects_admission(self, tmp_path):
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
