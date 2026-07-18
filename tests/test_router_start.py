"""nelix-3rm slice 3c.1 Part E: the START path (spec §3) — the whole point of the router.

The router allocates the session id BEFORE forwarding /start, records the chosen generation, forwards
with the assigned id, and records the result idempotently. A retried /start with the SAME
idempotency_key must NEVER spawn a second worker: a same-request retry replays the original outcome;
a same-key-DIFFERENT-request is an idempotency_conflict; a lost/failed forward is recorded and its
stable error is replayed.

The generation backend here is a small IN-PROCESS HTTP stub implementing the daemon's GET /health +
POST /start (accepting a router-assigned session_id) — the router's start path + idempotency are
unit-tested WITHOUT a real daemon. A real-daemon integration test lives in
test_router_start_realdaemon.py."""
import http.server
import json
import re
import threading

import pytest

import paths
from daemon.transport import Transport
from nelix_store.ledger import StartLedger
from router.registry import GenerationRegistry
from router.start import StartPath

from conftest import EXECUTOR, OWNER

_SID_RE = re.compile(r"^s-[0-9a-f]{32}$")


class _Backend:
    """In-process HTTP stub of a generation: GET /health + POST /start."""

    def __init__(self, *, mode="ok", build_id="b-1"):
        self.mode = mode
        self.build_id = build_id
        self.starts = []                      # every /start body the backend received
        backend = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    self._send(200, {"status": "ok", "rpc_protocol": 1,
                                     "generation_id": backend.build_id})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path != "/start":
                    self._send(404, {"error": "not found"}); return
                backend.starts.append(body)
                if backend.mode == "error":
                    self._send(409, {"error": "generation is full"}); return
                if backend.mode == "malformed":
                    # A 200 whose body is NOT valid JSON: the daemon answered (a worker may exist),
                    # but the reply cannot be decoded (finding #3).
                    raw = b"this is not json{"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw); return
                sid = body.get("session_id")
                self._send(200, {"operation": "start", "status": "started", "session_id": sid,
                                 "snapshot": {"session_id": sid, "control_state": "busy"},
                                 "next_after_seq": 0, "next_action": "end_turn"})

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    @property
    def transport(self):
        return Transport.tcp("127.0.0.1", self.port, "t")

    def close(self):
        self._srv.shutdown()


class _Supervisor:
    """A minimal supervisor stand-in whose active_generation()/current_generation() return a
    (transport, incarnation) pair — read together, as the registry consumes it."""

    def __init__(self, transport, inc=None):
        self._t = transport
        self.inc = inc or {"pid": 1, "start_fingerprint": "fp"}

    def active_generation(self):
        return (self._t, self.inc)

    def current_generation(self):
        return (self._t, self.inc)

    def ensure_running(self):
        return self._t


def _start_path(backend):
    ledger = StartLedger(paths.nelix_root())
    reg = GenerationRegistry(supervisor=_Supervisor(backend.transport),
                             health_probe=lambda t: backend.build_id)
    return StartPath(ledger, reg), ledger


def _body(**over):
    b = {"executor": EXECUTOR, "task": "do the thing", "cwd": "/repo", "owner_id": OWNER,
         "idempotency_key": "k-1"}
    b.update(over)
    return b


def test_fresh_start_reserves_assigns_forwards_and_commits():
    backend = _Backend()
    sp, ledger = _start_path(backend)
    try:
        status, resp = sp.handle(_body())
        assert status == 200
        assert resp["status"] == "started"
        sid = resp["session_id"]
        assert _SID_RE.match(sid)                         # router-minted wide id
        # The backend received EXACTLY the router-assigned id.
        assert len(backend.starts) == 1
        assert backend.starts[0]["session_id"] == sid
        # The ledger row committed to the generation epoch the response reports.
        row = ledger.lookup("k-1", owner_id=OWNER)
        assert row.state == "started"
        assert row.generation_id == resp["generation_id"]
        assert re.match(r"^g-[0-9a-f]{32}$", resp["generation_id"])
    finally:
        backend.close()


def test_retried_same_request_replays_and_does_not_forward_twice():
    backend = _Backend()
    sp, ledger = _start_path(backend)
    try:
        s1, r1 = sp.handle(_body())
        s2, r2 = sp.handle(_body())                       # identical retry, same idempotency_key
        assert s1 == 200 and s2 == 200
        assert r2["session_id"] == r1["session_id"]       # SAME session, not a new one
        assert r2.get("replay") is True
        assert len(backend.starts) == 1                   # NO second forward -> NO second worker
    finally:
        backend.close()


def test_same_key_different_request_is_an_idempotency_conflict():
    backend = _Backend()
    sp, _ = _start_path(backend)
    try:
        sp.handle(_body(task="task A"))
        status, resp = sp.handle(_body(task="task B"))    # same key, different task
        assert status == 409
        assert resp["error"]["code"] == "idempotency_conflict"
        assert resp["error"]["retryable"] is False
        assert len(backend.starts) == 1                   # the conflicting retry never forwarded
    finally:
        backend.close()


def test_generation_failure_fails_the_reservation_and_returns_a_stable_error():
    backend = _Backend(mode="error")
    sp, ledger = _start_path(backend)
    try:
        status, resp = sp.handle(_body())
        assert status == 503
        assert resp["error"]["code"] == "generation_unavailable"
        assert "error" in resp and "code" in resp["error"]     # a stable envelope, not a bare 500
        row = ledger.lookup("k-1", owner_id=OWNER)
        assert row.state == "failed"                            # durably recorded
        assert len(backend.starts) == 1
    finally:
        backend.close()


def test_retry_after_failure_replays_the_recorded_failure():
    backend = _Backend(mode="error")
    sp, _ = _start_path(backend)
    try:
        sp.handle(_body())
        status, resp = sp.handle(_body())                 # same key: replays the recorded failure
        assert status == 503
        assert resp["error"]["code"] == "generation_unavailable"
        assert len(backend.starts) == 1                   # NOT forwarded again
    finally:
        backend.close()


def test_generation_unavailable_when_no_backend_can_be_made():
    # A registry that cannot provide a generation at all -> GENERATION_UNAVAILABLE, and the
    # reservation is failed so a same-key retry replays the failure (never a fresh worker).
    class _DeadSupervisor:
        def active_generation(self): return None
        def current_generation(self): return None
        def ensure_running(self): raise RuntimeError("daemon did not become healthy")
    ledger = StartLedger(paths.nelix_root())
    sp = StartPath(ledger, GenerationRegistry(supervisor=_DeadSupervisor(),
                                              health_probe=lambda t: None))
    status, resp = sp.handle(_body())
    assert status == 503
    assert resp["error"]["code"] == "generation_unavailable"
    assert resp["error"]["retryable"] is True
    assert ledger.lookup("k-1", owner_id=OWNER).state == "failed"


def test_forward_to_a_dead_transport_is_generation_unavailable():
    class _Sup:
        _t = Transport.tcp("127.0.0.1", 9, "t")                          # discard port: refused
        def active_generation(self): return (self._t, {"pid": 1, "start_fingerprint": "fp"})
        def current_generation(self): return (self._t, {"pid": 1, "start_fingerprint": "fp"})
        def ensure_running(self): return self._t
    ledger = StartLedger(paths.nelix_root())
    sp = StartPath(ledger, GenerationRegistry(supervisor=_Sup(), health_probe=lambda t: None))
    status, resp = sp.handle(_body())
    assert status == 503
    assert resp["error"]["code"] == "generation_unavailable"
    assert ledger.lookup("k-1", owner_id=OWNER).state == "failed"


def test_response_phase_forward_leaves_reservation_starting_and_retry_replays(monkeypatch):
    # Findings #2/#3: a forward that fails in the RESPONSE phase (request SENT, then the reply timed
    # out / dropped / was malformed) is AMBIGUOUS — the daemon may already have created the worker.
    # It must NOT be recorded as a durable failure (the ledger's create-then-fail guard is inert for
    # a router forward: the daemon writes owner.json, not the store's sessions table). The reservation
    # stays `starting`, a same-key retry replays it in-progress (not a failure), and NO second worker
    # is forwarded.
    forwards = []

    class _ResponsePhaseClient:
        def __init__(self, transport, owner_id):
            pass

        def start(self, executor, task, cwd, model=None, session_id=None, timeout=30):
            forwards.append(session_id)          # the request left the router (worker may exist)
            from rpc_client import ForwardResponseError
            raise ForwardResponseError("read timed out")  # ...reply never arrived: AMBIGUOUS

    import rpc_client
    monkeypatch.setattr(rpc_client, "RpcClient", _ResponsePhaseClient)

    backend = _Backend()
    sp, ledger = _start_path(backend)
    try:
        status, resp = sp.handle(_body())
        assert status == 503
        assert resp["error"]["code"] == "generation_unavailable"
        assert resp["error"]["retryable"] is True
        row = ledger.lookup("k-1", owner_id=OWNER)
        assert row.state == "starting"               # NOT durably failed — still in-progress
        assert len(forwards) == 1
        # A same-key retry replays the in-progress reservation and does NOT forward again.
        status2, resp2 = sp.handle(_body())
        assert status2 == 200
        assert resp2["status"] == "starting"
        assert resp2.get("replay") is True
        assert len(forwards) == 1                     # NO second worker spawned
    finally:
        backend.close()


def test_connect_phase_forward_definitely_fails_and_retry_replays_the_failure(monkeypatch):
    # Findings #2: a connect-phase failure (here connection refused) is DEFINITE — the request never
    # left the router, so no worker was created. It IS recorded as a failure, and a same-key retry
    # replays that recorded failure (never a fresh worker).
    forwards = []

    class _ConnectPhaseClient:
        def __init__(self, transport, owner_id):
            pass

        def start(self, executor, task, cwd, model=None, session_id=None, timeout=30):
            forwards.append(session_id)
            from rpc_client import ForwardConnectError
            raise ForwardConnectError("connection refused")   # connect failed: worker not started

    import rpc_client
    monkeypatch.setattr(rpc_client, "RpcClient", _ConnectPhaseClient)

    backend = _Backend()
    sp, ledger = _start_path(backend)
    try:
        status, resp = sp.handle(_body())
        assert status == 503
        assert resp["error"]["code"] == "generation_unavailable"
        row = ledger.lookup("k-1", owner_id=OWNER)
        assert row.state == "failed"                 # DEFINITE: durably recorded as failed
        assert len(forwards) == 1
        status2, resp2 = sp.handle(_body())          # replays the recorded failure
        assert status2 == 503
        assert resp2["error"]["code"] == "generation_unavailable"
        assert len(forwards) == 1                     # NOT forwarded again
    finally:
        backend.close()


def test_pre_connect_oserror_is_definite_and_not_stranded(tmp_path):
    # Finding #2 (through the FULL StartPath, real RpcClient): a PRE-CONNECT OSError that is NOT
    # connection-refused (here NotADirectoryError — the unix socket path's parent is a regular file)
    # is a connect-phase failure -> DEFINITE. The request never left, so no worker exists, and it
    # must be recorded `failed`, NOT left ambiguous/`starting` forever (the old broad-OSError bug).
    afile = tmp_path / "not-a-dir"
    afile.write_text("x")
    dead = Transport.unix(str(afile / "gen.sock"))     # connect -> NotADirectoryError (an OSError)

    class _Sup:
        def active_generation(self): return (dead, {"pid": 1, "start_fingerprint": "fp"})
        def current_generation(self): return (dead, {"pid": 1, "start_fingerprint": "fp"})
        def ensure_running(self): return dead

    ledger = StartLedger(paths.nelix_root())
    sp = StartPath(ledger, GenerationRegistry(supervisor=_Sup(), health_probe=lambda t: None))
    status, resp = sp.handle(_body())
    assert status == 503
    assert resp["error"]["code"] == "generation_unavailable"
    row = ledger.lookup("k-1", owner_id=OWNER)
    assert row.state == "failed"                        # DEFINITE — NOT stranded in `starting`
    # A same-key retry replays the recorded failure (never a fresh worker).
    status2, resp2 = sp.handle(_body())
    assert status2 == 503
    assert resp2["error"]["code"] == "generation_unavailable"
    assert ledger.lookup("k-1", owner_id=OWNER).state == "failed"


def test_malformed_generation_reply_is_retryable_and_leaves_reservation_starting():
    # Finding #3 (through the FULL StartPath, real RpcClient): a generation whose /start reply is not
    # valid JSON is a RESPONSE-phase failure -> AMBIGUOUS. The daemon answered (a worker may exist),
    # so it is GENERATION_UNAVAILABLE (retryable) with the reservation left `starting`, and a same-key
    # retry replays it in-progress rather than forwarding a second time.
    backend = _Backend(mode="malformed")
    sp, ledger = _start_path(backend)
    try:
        status, resp = sp.handle(_body())
        assert status == 503
        assert resp["error"]["code"] == "generation_unavailable"
        assert resp["error"]["retryable"] is True
        assert ledger.lookup("k-1", owner_id=OWNER).state == "starting"   # NOT durably failed
        assert len(backend.starts) == 1
        status2, resp2 = sp.handle(_body())            # replays the in-progress reservation
        assert status2 == 200
        assert resp2["status"] == "starting"
        assert resp2.get("replay") is True
        assert len(backend.starts) == 1                # NO second worker forwarded
    finally:
        backend.close()


def test_missing_idempotency_key_is_rejected():
    backend = _Backend()
    sp, _ = _start_path(backend)
    try:
        b = _body(); del b["idempotency_key"]
        status, resp = sp.handle(b)
        assert status == 400
        assert resp["error"]["code"] == "invalid_request"
        assert len(backend.starts) == 0
    finally:
        backend.close()


def test_invalid_owner_id_is_rejected():
    backend = _Backend()
    sp, _ = _start_path(backend)
    try:
        status, resp = sp.handle(_body(owner_id="bad owner id!!"))
        assert status == 400
        assert resp["error"]["code"] == "invalid_request"
    finally:
        backend.close()


def test_no_orchestration_id_still_replays_on_retry():
    # A caller that supplies only an idempotency_key (no orchestration_id) must still get an
    # idempotent retry — the router derives a STABLE orchestration_id from (owner, key), so a retry
    # reproduces it and replays rather than conflicting on a freshly-minted one.
    backend = _Backend()
    sp, _ = _start_path(backend)
    try:
        s1, r1 = sp.handle(_body())
        s2, r2 = sp.handle(_body())
        assert s1 == 200 and s2 == 200
        assert r2["session_id"] == r1["session_id"]
        assert r2.get("replay") is True
        assert len(backend.starts) == 1
    finally:
        backend.close()


def test_concurrent_duplicate_starts_forward_once():
    backend = _Backend()
    sp, _ = _start_path(backend)
    try:
        results = []
        barrier = threading.Barrier(8)

        def _go():
            barrier.wait()
            results.append(sp.handle(_body()))

        threads = [threading.Thread(target=_go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        sids = {r[1].get("session_id") for r in results}
        assert len(sids) == 1                             # one session across all duplicates
        assert len(backend.starts) == 1                   # exactly ONE worker spawned
    finally:
        backend.close()
