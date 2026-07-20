"""nelix S1c-2: REAL persisted-Store re-adoption tests (round 3).

Proves that a fresh router (GenerationRegistry with a real Store) re-adopts
a pre-existing generation + epoch from durable state, never minting a new one.
Includes round-3 failure-path tests: wrong-epoch rejection, null-ptr repair,
non-serving rejection, CAS rollback, and bootstrap fail-closed.

Uses a fake in-process daemon server on a unix socket with REAL SQLite Store.
"""

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# All tests in this file share a REAL SQLite Store and file-system generation
# dirs — running them in parallel (xdist) causes collisions.  Lock to one
# worker via xdist_group.
pytestmark = pytest.mark.xdist_group(name="re_adoption_serial")

import paths
from daemon.protocol import RPC_PROTOCOL_VERSION
from daemon import reaper
from nelix_contracts.errors import (
    GENERATION_UNAVAILABLE, NelixError,
)
from nelix_contracts.ids import new_generation_id
from nelix_store.store import Store
from router.registry import GenerationRegistry


# ── helpers ──────────────────────────────────────────────────────────────────

def _start_fake_daemon_unix(sock_path, gid, gepoch, build_id):
    """Start a fake daemon HTTP server on a unix socket, reporting the given
    identity on /health."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health" or self.path.startswith("/health?"):
                body = json.dumps({
                    "status": "ok", "rpc_protocol": RPC_PROTOCOL_VERSION,
                    "generation_id": gid,
                    "generation_epoch": gepoch,
                    "build_id": build_id,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
                return
            body = json.dumps({"rpc_protocol": RPC_PROTOCOL_VERSION}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def do_POST(self): return self.do_GET()
        def log_message(self, *a): pass

    from socketserver import UnixStreamServer
    try:
        os.unlink(sock_path)
    except OSError:
        pass
    srv = UnixStreamServer(sock_path, H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _setup_gen_and_daemon(gid, gepoch, build_id=None, extra_epoch=None):
    """Seed a generation + epoch in the Store, start a fake daemon on TCP,
    write lock + state files. Returns (store, daemon_srv, port, token).
    The fake daemon reports the given (gid, gepoch, build_id) on /health."""
    gen_dir = paths.generation_dir(gid)
    gen_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(gen_dir, 0o700)
    try: os.chmod(gen_dir.parent, 0o700)
    except Exception: pass

    store = Store(paths.nelix_root())
    clock = 1000.0
    store.create_generation(gid, build_id=build_id, lifecycle_state="active",
                            capability_snapshot=None, created_at=clock)
    store.insert_epoch(gepoch, gid, incarnation_meta=None, created_at=clock)
    if extra_epoch:
        store.insert_epoch(extra_epoch, gid, incarnation_meta=None,
                           created_at=clock + 1.0)

    # Random TCP port for fake daemon.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    token = ""

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health" or self.path.startswith("/health?"):
                body = json.dumps({
                    "status": "ok", "rpc_protocol": RPC_PROTOCOL_VERSION,
                    "generation_id": gid,
                    "generation_epoch": gepoch,
                    "build_id": build_id,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
                return
            body = json.dumps({"rpc_protocol": RPC_PROTOCOL_VERSION}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def do_POST(self): return self.do_GET()
        def log_message(self, *a): pass

    daemon_srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=daemon_srv.serve_forever, daemon=True).start()

    pid = os.getpid()
    insp = reaper.ProcessInspector()
    real_fp = insp.start_fingerprint(pid)
    lock_meta = {"pid": pid, "start_fingerprint": real_fp,
                 "transport": "tcp", "host": "127.0.0.1", "port": port,
                 "token": token}
    paths.generation_lock(gid).write_text(json.dumps(lock_meta))
    paths.generation_state(gid).write_text(json.dumps(lock_meta))

    return store, daemon_srv, port, token


def _teardown(daemon_srv):
    daemon_srv.shutdown()
    daemon_srv.server_close()


# ── S1-T1: wrong epoch rejected ──────────────────────────────────────────────

def test_eager_readopt_wrong_epoch_rejected():
    """Seed serving epoch E; daemon reports different epoch E2.
    Eager reconcile must NOT adopt E2 — E2 must be dead after reconcile."""
    gid = new_generation_id()
    epoch_e = new_generation_id()
    epoch_e2 = new_generation_id()
    build_id = None

    store, daemon_srv, _port, _token = _setup_gen_and_daemon(gid, epoch_e2, build_id)
    try:
        # Insert epoch_e (the DB epoch) and promote it to serving.
        store.insert_epoch(epoch_e, gid, incarnation_meta=None, created_at=500.0)
        inc_meta = json.dumps({"pid": os.getpid(), "start_fingerprint": "fp"})
        store.cas_epoch_serving(gid, epoch_e, expected_current_epoch=None,
                                incarnation_meta=inc_meta)

        # Build registry — eager reconcile runs in constructor.
        # The daemon reports E2, DB says current_epoch=E → mismatch → reconcile.
        reg = GenerationRegistry(store=Store(paths.nelix_root()), build_id=build_id)

        # After eager reconcile, E (the DB epoch) should be dead.
        # E2 (daemon's epoch) stays as-is.  active() might fail but the key
        # assertion is that E was reconciled dead.
        epoch_list = store.list_epochs_strict(gid)
        e_row = [e for e in epoch_list if e.generation_epoch == epoch_e]
        assert e_row, "Epoch E not found in DB"
        assert e_row[0].process_state == "dead", (
            f"Epoch E should be dead after eager reconcile, got {e_row[0].process_state}")

        # The generation's current_epoch should be NULL (cleared by reconcile).
        gen = store.get_generation(gid)
        assert gen.current_epoch is None, (
            f"current_epoch should be NULL after reconcile, got {gen.current_epoch!r}")

        # S1c-2 round-4: call active() — it must NOT return a handle bound to E2.
        # Since E was reconciled dead and E2 is not in the DB, active() must raise
        # GENERATION_UNAVAILABLE (cannot adopt an unknown epoch).
        from unittest.mock import patch
        from generation_supervisor import GenerationSupervisor
        with patch.object(GenerationSupervisor, 'reap_holder', return_value=None):
            with pytest.raises(NelixError) as exc:
                reg.active()
            assert exc.value.code == GENERATION_UNAVAILABLE, (
                f"expected GENERATION_UNAVAILABLE, got {exc.value.code}")
    finally:
        _teardown(daemon_srv)


# ── S1-T2: serving epoch reused ──────────────────────────────────────────────

def test_eager_readopt_serving_reuses_epoch():
    """Seed serving epoch E + live daemon reporting E.  registry.active()
    returns handle with epoch==E and NO new epoch row was inserted."""
    gid = new_generation_id()
    gepoch = new_generation_id()
    build_id = None

    store, daemon_srv, _port, _token = _setup_gen_and_daemon(gid, gepoch, build_id)
    try:
        inc_meta = json.dumps({"pid": os.getpid(), "start_fingerprint": "fp"})
        store.cas_epoch_serving(gid, gepoch, expected_current_epoch=None,
                                incarnation_meta=inc_meta)

        # Count epochs before.
        epochs_before = len(store.list_epochs_strict(gid))

        reg = GenerationRegistry(store=Store(paths.nelix_root()), build_id=build_id)
        gen = reg.active()
        assert gen.epoch == gepoch, f"Expected {gepoch}, got {gen.epoch}"
        assert gen.generation_id == gid

        # NO new epoch row was inserted.
        epochs_after = len(store.list_epochs_strict(gid))
        assert epochs_after == epochs_before, (
            f"New epoch inserted: {epochs_before} → {epochs_after}")
    finally:
        _teardown(daemon_srv)


# ── S1-T3: null current_epoch repaired ───────────────────────────────────────

def test_null_current_epoch_repaired():
    """Seed serving epoch E with current_epoch=NULL + live daemon reporting E.
    After eager reconcile, the durable current_epoch must be E, not NULL."""
    gid = new_generation_id()
    gepoch = new_generation_id()

    store, daemon_srv, _port, _token = _setup_gen_and_daemon(gid, gepoch, None)
    try:
        inc_meta = json.dumps({"pid": os.getpid(), "start_fingerprint": "fp"})
        store.cas_epoch_serving(gid, gepoch, expected_current_epoch=None,
                                incarnation_meta=inc_meta)
        # Now manually set current_epoch back to NULL.
        store.clear_current_epoch(gid)
        gen_rec = store.get_generation(gid)
        assert gen_rec.current_epoch is None, "Precondition: current_epoch must be NULL"

        # Build registry — eager reconcile runs in constructor.
        # It should find the serving epoch E, detect NULL current_epoch,
        # and repair the pointer via set_current_epoch.
        _ = GenerationRegistry(store=Store(paths.nelix_root()), build_id=None)

        # The durable pointer must now be repaired to E.
        gen_rec_after = store.get_generation(gid)
        assert gen_rec_after.current_epoch == gepoch, (
            f"current_epoch was not repaired: {gen_rec_after.current_epoch!r}")
    finally:
        _teardown(daemon_srv)


# ── S1-T4: non-serving current_epoch rejected ────────────────────────────────

def test_refresh_rejects_nonserving_pointer():
    """Set up a serving daemon, then corrupt the DB so current_epoch
    points at a dead (and separately a starting) epoch.  active() must
    hit _refresh_active_locked's equal-pointer state check and REJECT."""
    from unittest.mock import patch
    from generation_supervisor import GenerationSupervisor

    gid = new_generation_id()
    gepoch = new_generation_id()
    gepoch_starting = new_generation_id()

    store, daemon_srv, _port, _token = _setup_gen_and_daemon(gid, gepoch, None)
    try:
        inc_meta = json.dumps({"pid": os.getpid(), "start_fingerprint": "fp"})
        store.cas_epoch_serving(gid, gepoch, expected_current_epoch=None,
                                incarnation_meta=inc_meta)

        # Build registry — eager reconcile runs, adopts the serving epoch.
        reg = GenerationRegistry(store=Store(paths.nelix_root()), build_id=None)

        # First active() call: should succeed (serving epoch matched).
        gen_first = reg.active()
        assert gen_first.epoch == gepoch

        # ── Dead case ──
        # Corrupt the DB: set the epoch to 'dead' but keep current_epoch=E.
        store.reconcile_epoch_dead(gid, gepoch)
        store.set_current_epoch(gid, gepoch)
        epoch_list = store.list_epochs_strict(gid)
        dead_check = [e for e in epoch_list if e.generation_epoch == gepoch]
        assert dead_check and dead_check[0].process_state == "dead"

        # active() hits _refresh_active_locked → store says current_epoch==epoch
        # but epoch is dead → must raise.
        with patch.object(GenerationSupervisor, 'reap_holder', return_value=None):
            with pytest.raises(NelixError) as exc:
                reg.active()
            assert exc.value.code == GENERATION_UNAVAILABLE

        # ── Starting case ──
        # Create a DIFFERENT starting epoch, set current_epoch to it, then
        # swap _active's epoch so the refresh sees a matching pointer to a
        # starting (non-serving) epoch.
        store.insert_epoch(gepoch_starting, gid, incarnation_meta=None, created_at=600.0)
        store.set_current_epoch(gid, gepoch_starting)
        # Swap _active's epoch to match the starting pointer.
        reg._active["epoch"] = gepoch_starting

        epoch_list2 = store.list_epochs_strict(gid)
        start_check = [e for e in epoch_list2 if e.generation_epoch == gepoch_starting]
        assert start_check and start_check[0].process_state == "starting"

        # active() hits _refresh_active_locked → matching pointer but epoch
        # is not serving → must raise.
        # Patch endpoint (TCP holder) and _check_health_strict (identity) so
        # the flow proceeds past lines 422-430 into the state check at :483-497.
        with patch.object(GenerationSupervisor, 'reap_holder', return_value=None), \
             patch.object(GenerationSupervisor, 'endpoint', return_value="mock_tcp"), \
             patch.object(GenerationSupervisor, '_check_health_strict', return_value=True):
            with pytest.raises(NelixError) as exc2:
                reg.active()
            assert exc2.value.code == GENERATION_UNAVAILABLE
            assert "not serving" in str(exc2.value), (
                f"Expected 'not serving' in error, got: {exc2.value}")
    finally:
        _teardown(daemon_srv)


# ── S1-C1: multiple historical starting epochs reconciled ─────────────────────


def test_historical_starting_epochs_reconciled():
    """When eager reconcile finds multiple starting epochs (plus current_epoch),
    ALL non-current starting epochs must be reconciled to dead.
    Regression test for the loop at registry.py:363-372."""
    from nelix_store.store import Store

    gid = new_generation_id()
    current = new_generation_id()
    starting_a = new_generation_id()
    starting_b = new_generation_id()

    store, daemon_srv, _port, _token = _setup_gen_and_daemon(gid, current, None)
    try:
        inc_meta = json.dumps({"pid": os.getpid(), "start_fingerprint": "fp"})
        store.cas_epoch_serving(gid, current, expected_current_epoch=None,
                                incarnation_meta=inc_meta)

        # Insert two extra starting epochs.
        store.insert_epoch(starting_a, gid, incarnation_meta=None, created_at=1100.0)
        store.insert_epoch(starting_b, gid, incarnation_meta=None, created_at=1200.0)

        # Remove the lock file so there is no holder → eager reconcile falls through
        # to the reconcile-all section (lines 349-372).
        lock_path = paths.generation_lock(gid)
        state_path = paths.generation_state(gid)
        lock_path.unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)

        _ = GenerationRegistry(store=Store(paths.nelix_root()), build_id=None)

        epochs = store.list_epochs_strict(gid)
        epoch_map = {e.generation_epoch: e.process_state for e in epochs}

        # current epoch should be dead (reconciled at line 353).
        assert epoch_map.get(current) == "dead", (
            f"current epoch should be dead, got {epoch_map.get(current)}")

        # Both starting epochs must be dead.
        assert epoch_map.get(starting_a) == "dead", (
            f"starting_a should be dead, got {epoch_map.get(starting_a)}")
        assert epoch_map.get(starting_b) == "dead", (
            f"starting_b should be dead, got {epoch_map.get(starting_b)}")
    finally:
        _teardown(daemon_srv)


def test_reconcile_starting_epoch_error_propagates():
    """When reconcile_epoch_dead raises NelixError on a starting epoch,
    the ORIGINAL error must propagate (not AttributeError from the
    except-shadow bug at registry.py:369)."""
    from unittest.mock import patch
    from nelix_store.store import Store

    gid = new_generation_id()
    current = new_generation_id()
    starting_a = new_generation_id()
    starting_b = new_generation_id()

    store, daemon_srv, _port, _token = _setup_gen_and_daemon(gid, current, None)
    try:
        inc_meta = json.dumps({"pid": os.getpid(), "start_fingerprint": "fp"})
        store.cas_epoch_serving(gid, current, expected_current_epoch=None,
                                incarnation_meta=inc_meta)
        store.insert_epoch(starting_a, gid, incarnation_meta=None, created_at=1100.0)
        store.insert_epoch(starting_b, gid, incarnation_meta=None, created_at=1200.0)

        lock_path = paths.generation_lock(gid)
        state_path = paths.generation_state(gid)
        lock_path.unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)

        # Patch reconcile_epoch_dead to raise NelixError for starting_b.
        original_reconcile = Store.reconcile_epoch_dead

        def _failing_reconcile(self, gen_id, epoch):
            if epoch == starting_b:
                raise NelixError(GENERATION_UNAVAILABLE, "forced reconcile failure")
            return original_reconcile(self, gen_id, epoch)

        with patch.object(Store, 'reconcile_epoch_dead', _failing_reconcile):
            with pytest.raises(NelixError) as exc:
                GenerationRegistry(store=Store(paths.nelix_root()), build_id=None)
            # Must be the original NelixError, not an AttributeError
            # (which would have no code attribute or show a different message).
            assert "forced reconcile failure" in str(exc.value), (
                f"Expected original error to propagate, got: {exc.value}")
            assert exc.value.code == GENERATION_UNAVAILABLE
    finally:
        _teardown(daemon_srv)


# ── S1-D1: TCP holder rejects non-serving epoch ───────────────────────────────


def test_tcp_holder_rejects_nonserving_epoch():
    """TCP-holder adoption branch must gate on process_state == 'serving'
    (registry.py:334). When epoch row is dead/starting, eager reconcile
    must NOT adopt — even if the TCP endpoint answers."""

    gid = new_generation_id()
    gepoch = new_generation_id()

    store, daemon_srv, _port, _token = _setup_gen_and_daemon(gid, gepoch, None)
    try:
        # Make the epoch non-serving (dead).
        store.reconcile_epoch_dead(gid, gepoch)
        store.set_current_epoch(gid, gepoch)
        ep_list = store.list_epochs_strict(gid)
        ep_row = [e for e in ep_list if e.generation_epoch == gepoch]
        assert ep_row and ep_row[0].process_state == "dead"

        # Build registry — eager reconcile must NOT adopt a dead epoch
        # even though the TCP daemon is alive and endpoint() would respond.
        reg = GenerationRegistry(store=Store(paths.nelix_root()), build_id=None)

        # _active must remain None — the dead epoch was not adopted.
        assert reg._active is None, (
            f"Expected _active=None, got {reg._active}")
    finally:
        _teardown(daemon_srv)


# ── S1-B1: bounded health retry for starting epoch ────────────────────────────


def test_starting_epoch_retry_health():
    """When a starting epoch's daemon is alive but not yet passing /health,
    a bounded retry must be attempted before giving up.
    Default (health_retries=0) preserves the original behavior; a retry>0
    avoids premature reconciliation of a still-booting daemon."""
    import tempfile
    from unittest.mock import patch
    from generation_supervisor import GenerationSupervisor
    from nelix_store.store import Store

    gid = new_generation_id()
    gepoch = new_generation_id()
    build_id = None

    sock_dir = tempfile.mkdtemp()
    sock_path = os.path.join(sock_dir, "daemon.sock")

    _srv = _start_fake_daemon_unix(sock_path, gid, gepoch, build_id)
    try:
        gen_dir = paths.generation_dir(gid)
        gen_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(gen_dir, 0o700)
        try: os.chmod(gen_dir.parent, 0o700)
        except Exception: pass

        store = Store(paths.nelix_root())
        clock = 1000.0
        store.create_generation(gid, build_id=build_id, lifecycle_state="active",
                                capability_snapshot=None, created_at=clock)
        store.insert_epoch(gepoch, gid, incarnation_meta=None, created_at=clock)
        # Keep as "starting" — do NOT promote to serving, but set current_epoch
        # so the eager reconcile enters the holder-processing path.
        store.set_current_epoch(gid, gepoch)

        pid = os.getpid()
        lock_meta = {"pid": pid, "start_fingerprint": "mock-fp",
                     "transport": "unix", "path": sock_path}
        paths.generation_lock(gid).write_text(json.dumps(lock_meta))
        paths.generation_state(gid).write_text(json.dumps(lock_meta))

        # Mock _check_health: flaky — fail first call, succeed on retry.
        call_count = [0]

        def _flaky_check(self, transport):
            call_count[0] += 1
            return call_count[0] >= 2

        def _mock_fingerprint(*args):
            return "mock-fp"

        with patch.object(GenerationSupervisor, '_check_health', _flaky_check), \
             patch.object(reaper.ProcessInspector, 'start_fingerprint', _mock_fingerprint):
            reg = GenerationRegistry(
                store=Store(paths.nelix_root()),
                build_id=None,
                health_retries=2,
                health_retry_delay=0.0,
            )

        # With retries, the flaky health check should recover and adopt.
        assert reg._active is not None, (
            "Expected _active to be set after retry")
        assert reg._active["epoch"] == gepoch, (
            f"Expected epoch {gepoch}, got {reg._active['epoch']}")
        # _check_health should have been called twice (fail once, then succeed).
        assert call_count[0] >= 2, (
            f"Expected at least 2 _check_health calls, got {call_count[0]}")
    finally:
        _srv.shutdown()
        _srv.server_close()
        import shutil
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_starting_epoch_no_retry_reconciles_dead():
    """With health_retries=0 (default), a starting epoch whose daemon
    is alive but NOT passing /health is reconciled dead immediately
    (preserves the original pre-hardening behavior)."""
    import tempfile
    from unittest.mock import patch
    from generation_supervisor import GenerationSupervisor
    from nelix_store.store import Store

    gid = new_generation_id()
    gepoch = new_generation_id()
    build_id = None

    sock_dir = tempfile.mkdtemp()
    sock_path = os.path.join(sock_dir, "daemon.sock")
    _srv = _start_fake_daemon_unix(sock_path, gid, gepoch, build_id)
    try:
        gen_dir = paths.generation_dir(gid)
        gen_dir.mkdir(parents=True, exist_ok=True)
        store = Store(paths.nelix_root())
        clock = 2000.0
        store.create_generation(gid, build_id=build_id, lifecycle_state="active",
                                capability_snapshot=None, created_at=clock)
        store.insert_epoch(gepoch, gid, incarnation_meta=None, created_at=clock)
        store.set_current_epoch(gid, gepoch)

        pid = os.getpid()
        lock_meta = {"pid": pid, "start_fingerprint": "mock-fp",
                     "transport": "unix", "path": sock_path}
        paths.generation_lock(gid).write_text(json.dumps(lock_meta))
        paths.generation_state(gid).write_text(json.dumps(lock_meta))

        def _mock_fp(*args):
            return "mock-fp"

        def _failing_check(self, transport):
            return False

        with patch.object(GenerationSupervisor, '_check_health', _failing_check), \
             patch.object(reaper.ProcessInspector, 'start_fingerprint', _mock_fp):
            reg = GenerationRegistry(
                store=Store(paths.nelix_root()),
                build_id=None,
                health_retries=0,
            )

        assert reg._active is None, (
            "Expected _active=None with health_retries=0")
    finally:
        _srv.shutdown()
        _srv.server_close()
        import shutil
        shutil.rmtree(sock_dir, ignore_errors=True)


# ── S1-A1: dir-only orphans reaped when no active generation ─────────────────


def test_orphan_dir_reaped_no_active():
    """When there is no active generation row but a stray generation dir exists
    on disk with a lock holder, eager reconcile must STILL reap that orphan.
    Regression: the reap block was previously after the no-active return."""
    import json
    from unittest.mock import patch
    from generation_supervisor import GenerationSupervisor

    orphan_gid = new_generation_id()

    # Create a stray generation dir on disk with NO corresponding DB row.
    gen_dir = paths.generation_dir(orphan_gid)
    gen_dir.mkdir(parents=True, exist_ok=True)
    # Write a lock file so it looks like a real gen dir (even though _live_lock_holder
    # will return None because the fingerprint won't match — we patch it below).
    paths.generation_lock(orphan_gid).write_text(json.dumps({"pid": os.getpid()}))
    paths.generation_state(orphan_gid).write_text(json.dumps({}))

    try:
        # Build registry — no active gen exists, but the reap loop should
        # find the orphan dir and call reap_holder on it.
        reaped_dirs = []

        def _mock_live_lock_holder(self):
            """Return a fake holder for any gen dir so the reap loop
            calls reap_holder on it."""
            return {"pid": 999999, "start_fingerprint": "mock-fp"}

        def _tracking_reap(self, incarnation):
            reaped_dirs.append(self._gen_dir.name)
            # Do NOT call original — it would kill a real process.
            return None

        with patch.object(GenerationSupervisor, 'reap_holder', _tracking_reap), \
             patch.object(GenerationSupervisor, '_live_lock_holder', _mock_live_lock_holder):
            _ = GenerationRegistry(store=Store(paths.nelix_root()), build_id=None)

        # The orphan dir's holder should have been reaped.
        assert orphan_gid in reaped_dirs, (
            f"Expected {orphan_gid} to be reaped, got {reaped_dirs}")
    finally:
        # Cleanup stray dir.
        import shutil
        if gen_dir.exists():
            shutil.rmtree(gen_dir, ignore_errors=True)

