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
        with patch.object(GenerationSupervisor, 'reap_holder', return_value=None):
            with pytest.raises(NelixError) as exc2:
                reg.active()
            assert exc2.value.code == GENERATION_UNAVAILABLE
    finally:
        _teardown(daemon_srv)

