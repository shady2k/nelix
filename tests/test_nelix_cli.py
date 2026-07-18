"""nelix-3rm slice 3d: the minimal `nelix` CLI (daemon ensure|status|wait — spec Implementation-
plans §3, §11 "the wake executable"). nelix_cli.py at the repo root is a CLIENT + LAUNCHER only:
these tests never assert on router/daemon BEHAVIOR (that belongs to router/'s own suite), only
that the CLI's argparse surface, its ROUTER spawn/health-check (mirroring supervisor.py's shape),
and its status/wait wrappers do what they claim against a REAL router.

Two flavors of "real" are used, matching this repo's own established idiom:
  * `real_router` — a REAL `python -m router.app` SUBPROCESS, brought up the exact way
    `nelix daemon ensure` does. Proves ensure's spawn/health-check/idempotency and status/wait's
    plumbing against no daemon (both routes this exercises -- the board fan-out and the
    ledger-first empty-orchestration short-circuit -- are non-spawning, so no daemon is needed).
  * `router_over_real_daemon` — mirrors tests/test_router_wait_realdaemon.py's harness: a REAL
    daemon (real SessionManager + real rpc_server, PTY faked) behind the FULL router stack,
    bound at the SAME paths.router_sock() nelix_cli resolves. Proves status's owner filtering and
    wait's real-event wake through the CLI's own wrappers, against real ownership + a real event
    ring -- not a fabricated frame.

Every test that spawns a REAL router process cleans it up (SIGTERM/kill + wait, then the leaf
runtime dir is removed) so nothing leaks across tests or blocks the suite.
"""
import hashlib
import json
import os
import shutil
import subprocess
import threading

import pytest

import nelix_cli
import paths
from conftest import EXECUTOR, OWNER, make_spec
from daemon.events import EventQueue
from daemon.launchers.base import ExecutorCapabilities
from daemon.manager import SessionManager
from daemon.rpc_server import make_server
from daemon.transport import Transport
from nelix_store.ledger import StartLedger
from router import runtime_dir as rd
from router.registry import GenerationRegistry
from router.server import make_router_server
from router.start import StartPath
from rpc_client import RpcClient

from _router_fakes import Supervisor

OTHER_OWNER = "harness-y"
_VALID_ORCH = "o-" + "1" * 32


# --------------------------------------------------------------------------------------------
# argparse surface: bad subcommand -> usage + non-zero; help works. No router involved.
# --------------------------------------------------------------------------------------------

def test_no_command_exits_nonzero_with_usage(capsys):
    with pytest.raises(SystemExit) as ei:
        nelix_cli.main([])
    assert ei.value.code != 0
    assert "usage:" in capsys.readouterr().err


def test_unknown_top_level_command_exits_nonzero_with_usage(capsys):
    with pytest.raises(SystemExit) as ei:
        nelix_cli.main(["bogus"])
    assert ei.value.code != 0
    assert "usage:" in capsys.readouterr().err


def test_daemon_without_a_subcommand_exits_nonzero_with_usage(capsys):
    with pytest.raises(SystemExit) as ei:
        nelix_cli.main(["daemon"])
    assert ei.value.code != 0
    assert "usage:" in capsys.readouterr().err


def test_unknown_daemon_subcommand_exits_nonzero_with_usage(capsys):
    with pytest.raises(SystemExit) as ei:
        nelix_cli.main(["daemon", "bogus"])
    assert ei.value.code != 0
    assert "usage:" in capsys.readouterr().err


def test_status_without_owner_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as ei:
        nelix_cli.main(["daemon", "status"])
    assert ei.value.code != 0
    assert "--owner" in capsys.readouterr().err


def test_top_level_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as ei:
        nelix_cli.main(["--help"])
    assert ei.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_daemon_wait_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as ei:
        nelix_cli.main(["daemon", "wait", "--help"])
    assert ei.value.code == 0
    assert "--orchestration" in capsys.readouterr().out


# --------------------------------------------------------------------------------------------
# `ensure` against a REAL router subprocess: probe -> spawn -> health-check -> idempotent reuse.
# --------------------------------------------------------------------------------------------

@pytest.fixture
def real_router(monkeypatch):
    """Spies on every subprocess.Popen call (so a test can assert exactly how many router
    processes were spawned) and guarantees cleanup: SIGTERM/kill each spawned process and remove
    the leaf runtime dir, so a router `ensure` brings up never survives the test."""
    spawned = []
    real_popen = subprocess.Popen

    def _spy(*a, **kw):
        p = real_popen(*a, **kw)
        spawned.append(p)
        return p

    monkeypatch.setattr(subprocess, "Popen", _spy)
    try:
        yield spawned
    finally:
        for p in spawned:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        shutil.rmtree(paths.router_sock().parent, ignore_errors=True)


def test_ensure_spawns_a_real_router_and_it_becomes_healthy(real_router, capsys):
    assert paths.router_sock().exists() is False           # nothing running yet

    rc = nelix_cli.main(["daemon", "ensure"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["spawned"] is True
    assert out["pid"] == real_router[0].pid
    assert out["endpoint"] == str(paths.router_sock())
    assert paths.router_sock().exists()
    assert len(real_router) == 1


def test_a_second_ensure_is_idempotent_and_spawns_no_second_router(real_router, capsys):
    rc1 = nelix_cli.main(["daemon", "ensure"])
    capsys.readouterr()
    assert rc1 == 0
    assert len(real_router) == 1

    rc2 = nelix_cli.main(["daemon", "ensure"])
    out2 = json.loads(capsys.readouterr().out)

    assert rc2 == 0
    assert out2["spawned"] is False
    assert "pid" not in out2                                # we did not spawn it, so we don't know it
    assert len(real_router) == 1                            # NOT a second router


def test_ensure_router_recovers_when_its_own_spawn_loses_the_lock_race(monkeypatch):
    """The router's own establish() serializes concurrent spawns via its exclusive flock: if two
    `ensure`s race, the LOSER's spawned router exits code 3 (RouterLockHeld) while the winner
    keeps serving. ensure_router must treat that as success (a healthy router now exists, just not
    one WE spawned) -- mirrors supervisor.ensure_running's "lost the startup race, reusing
    pid-holder" recovery -- rather than raising RuntimeError."""
    healths = iter([
        None,                                                       # top-of-function probe
        None,                                                       # first loop iteration: not yet
        {"status": "ok", "router_epoch": "r-x", "active_generation": None},  # post-exit recovery probe
    ])
    monkeypatch.setattr(nelix_cli, "_router_health", lambda timeout=2: next(healths))

    class _FakeExitedProc:
        pid = 999999
        returncode = 3

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _FakeExitedProc())

    result = nelix_cli.ensure_router(timeout=2)

    assert result["spawned"] is False
    assert result["pid"] is None
    assert result["health"]["router_epoch"] == "r-x"


def test_ensure_router_keeps_polling_past_a_lost_lock_race_until_the_winner_is_healthy(
        monkeypatch):
    """A single immediate re-probe after the spawned proc exits (RouterLockHeld) is spurious: the
    WINNER may still be INITIALIZING and not yet serving /health at that exact instant. ensure_router
    must keep polling /health up to the ORIGINAL deadline rather than failing on one re-probe --
    only a timeout with nothing healthy by the deadline is a genuine failure."""
    healths = iter([
        None,                                                       # top-of-function probe
        None,                                                       # loop iter 1 (also observes the exit)
        None,                                                       # loop iter 2: still not up
        None,                                                       # loop iter 3: still not up -- the OLD
                                                                      # single-re-probe code would already
                                                                      # have raised by here
        {"status": "ok", "router_epoch": "r-y", "active_generation": None},  # loop iter 4: winner is up
    ])
    monkeypatch.setattr(nelix_cli, "_router_health", lambda timeout=2: next(healths))

    class _FakeExitedProc:
        pid = 999997
        returncode = 3

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _FakeExitedProc())

    result = nelix_cli.ensure_router(timeout=2)

    assert result["spawned"] is False
    assert result["pid"] is None
    assert result["health"]["router_epoch"] == "r-y"


def test_ensure_router_raises_when_the_spawn_never_becomes_healthy(monkeypatch):
    monkeypatch.setattr(nelix_cli, "_router_health", lambda timeout=2: None)

    class _FakeHangingProc:
        """Simulates a truly stuck child: terminate() is requested but the process does not die
        until kill() is also applied, so this exercises BOTH the wait(timeout=...) escalation and
        the final kill()."""
        pid = 999998

        def __init__(self):
            self.terminated = False
            self.killed = False
            self._dead = False

        def poll(self):
            return 0 if self._dead else None                # never exits on its own

        def terminate(self):
            self.terminated = True                           # requested, but does not actually die

        def wait(self, timeout=None):
            if not self._dead:
                raise subprocess.TimeoutExpired(cmd="router", timeout=timeout)
            return 0

        def kill(self):
            self.killed = True
            self._dead = True

    proc = _FakeHangingProc()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: proc)

    with pytest.raises(RuntimeError, match="did not become healthy"):
        nelix_cli.ensure_router(timeout=0.3)

    assert proc.terminated is True                          # cleanup was attempted...
    assert proc.killed is True                              # ...and escalated when it didn't exit


# --------------------------------------------------------------------------------------------
# `status` / `wait` against a REAL router subprocess with no daemon behind it: both routes this
# exercises (the board fan-out, and wait's ledger-first empty-orchestration short-circuit) are
# non-spawning, so this proves the CLI's wrappers without needing a live daemon.
# --------------------------------------------------------------------------------------------

def test_status_hints_to_run_ensure_when_no_router_is_running(capsys):
    rc = nelix_cli.main(["daemon", "status", "--owner", "owner-x"])
    assert rc == 1
    assert "nelix daemon ensure" in capsys.readouterr().err


def test_wait_hints_to_run_ensure_when_no_router_is_running(capsys):
    rc = nelix_cli.main(["daemon", "wait", "--owner", "owner-x", "--orchestration", _VALID_ORCH])
    assert rc == 1
    assert "nelix daemon ensure" in capsys.readouterr().err


def test_status_reports_the_empty_board_via_a_real_router_with_no_daemon(real_router, capsys):
    assert nelix_cli.main(["daemon", "ensure"]) == 0
    capsys.readouterr()

    rc = nelix_cli.main(["daemon", "status", "--owner", "owner-x"])

    assert rc == 0
    body = json.loads(capsys.readouterr().out)
    assert body == {"sessions": {}, "recent_terminal": {}, "cursor": body["cursor"],
                    "board_incomplete": False}


def test_wait_prints_the_explicit_empty_orchestration_signal(real_router, capsys):
    assert nelix_cli.main(["daemon", "ensure"]) == 0
    capsys.readouterr()

    rc = nelix_cli.main(["daemon", "wait", "--owner", "owner-x", "--orchestration", _VALID_ORCH])

    assert rc == 0
    body = json.loads(capsys.readouterr().out)
    assert body == {"event": None, "empty_orchestration": True}


# --------------------------------------------------------------------------------------------
# `status` / `wait` against a REAL daemon (real SessionManager + real rpc_server, PTY faked)
# behind the FULL router stack -- mirrors tests/test_router_wait_realdaemon.py's harness, bound
# at the SAME paths.router_sock() nelix_cli resolves, so the CLI never knows the difference
# between this and a subprocess-spawned router.
# --------------------------------------------------------------------------------------------

class _StubDriver:
    hook_capable = True


class _StubLauncher:
    capabilities = ExecutorCapabilities(isolation_class="host", can_attach=False)


class _FakeSession:
    _cols = 80
    _rows = 24

    def __init__(self, sid, executor):
        self.sid = sid
        self.executor = executor
        self.task = self.cwd = None
        self._driver = _StubDriver()
        self._launcher = _StubLauncher()

    def start(self, task, cwd):
        self.task, self.cwd = task, cwd

    def snapshot(self):
        return {"session_id": self.sid, "executor": self.executor,
                "control_state": "busy", "task_delivery": "pending", "pending": False}

    def is_working(self):
        return False

    def stop(self):
        pass


class _RealDaemon:
    def __init__(self, sock_path):
        self.created = {}
        daemon = self

        def factory(sid, executor, spec, events):
            s = _FakeSession(sid, executor)
            daemon.created[sid] = s
            return s

        self.events = EventQueue()
        self.manager = SessionManager({EXECUTOR: make_spec()}, self.events,
                                      session_factory=factory, concurrency_limit=5)
        self.server = make_server(self.manager, Transport.unix(sock_path))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.transport = Transport.unix(sock_path)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def daemon_sock(tmp_path):
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:8]
    p = f"/tmp/nxcli{h}.sock"
    yield p
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass


@pytest.fixture
def router_over_real_daemon(daemon_sock, tmp_path):
    daemon = _RealDaemon(daemon_sock)
    router = rd.establish()
    ledger = StartLedger(paths.nelix_root())
    registry = GenerationRegistry(supervisor=Supervisor(daemon.transport))
    epoch = "r-" + "0" * 32
    server = make_router_server(router.socket, router.sock_path,
                                StartPath(ledger, registry), registry, epoch)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield daemon, tmp_path
    finally:
        server.shutdown()
        router.close()
        daemon.close()
        ledger.close()


def _start(tmp_path, owner_id, key, orch=None):
    client = RpcClient(Transport.unix(str(paths.router_sock())), owner_id)
    payload = {"executor": EXECUTOR, "task": "do the work", "cwd": str(tmp_path),
              "owner_id": owner_id, "idempotency_key": key}
    if orch is not None:
        payload["orchestration_id"] = orch
    st, body = client._call("POST", "/start", payload)
    assert st == 200, body
    return body["session_id"]


def test_status_is_owner_filtered_through_the_cli_against_a_real_daemon(
        router_over_real_daemon, capsys):
    daemon, tmp_path = router_over_real_daemon
    mine = _start(tmp_path, OWNER, "k-cli-owner")
    theirs = _start(tmp_path, OTHER_OWNER, "k-cli-other")

    rc = nelix_cli.main(["daemon", "status", "--owner", OWNER])

    assert rc == 0
    body = json.loads(capsys.readouterr().out)
    assert mine in body["sessions"]
    assert theirs not in body["sessions"]
    assert len(body["sessions"]) == 1


def test_wait_wakes_on_a_real_event_through_the_cli_against_a_real_daemon(
        router_over_real_daemon, capsys, monkeypatch):
    """Proves the BLOCKED-wake path, not mere replay: `wait` is armed and PARKED inside the
    daemon's real blocking primitive (EventQueue._cv.wait) with no event yet on the ring, and only
    THEN -- from another thread -- does a real event get published onto the real daemon's event
    ring. The CLI's `wait` (run synchronously in its own thread, exactly as a real caller invokes
    it) must wake on that later event and return it.

    The `_cv.wait` instance is instrumented (not the daemon's wake LOGIC) purely to observe timing:
    it signals the instant the waiting thread is about to park, and only then delegates to the
    real wait. Because publish() takes the SAME lock to notify, the main thread cannot get past
    that lock and call publish() before the waiting thread has actually released it by parking --
    so "signalled" here is equivalent to "genuinely blocked", with no sleep-based guessing."""
    daemon, tmp_path = router_over_real_daemon
    orch = "o-" + "c" * 32
    sid = _start(tmp_path, OWNER, "k-cli-wait", orch)

    assert nelix_cli.main(["daemon", "status", "--owner", OWNER]) == 0
    cursor = json.loads(capsys.readouterr().out)["cursor"]

    blocked = threading.Event()
    real_cv_wait = daemon.events._cv.wait

    def _spy_wait(timeout=None):
        blocked.set()
        return real_cv_wait(timeout)

    monkeypatch.setattr(daemon.events._cv, "wait", _spy_wait)

    result = {}

    def _run_wait():
        result["rc"] = nelix_cli.main(["daemon", "wait", "--owner", OWNER,
                                       "--orchestration", orch, "--cursor", cursor])

    waiter = threading.Thread(target=_run_wait, daemon=True)
    waiter.start()
    try:
        assert blocked.wait(timeout=5), "the CLI wait never reached the blocking primitive"

        # A REAL event, published ONLY after the wait is confirmed blocked -- not before.
        evt = daemon.events.publish(sid, EXECUTOR, "waiting_for_user", "answer me",
                                    "waiting_for_user")

        waiter.join(timeout=10)
        assert not waiter.is_alive(), "the CLI wait never woke on the published event"
    finally:
        waiter.join(timeout=5)                              # never leak a running waiter thread

    assert result["rc"] == 0
    body = json.loads(capsys.readouterr().out)
    assert body["event"]["session_id"] == sid
    assert body["event"]["seq"] == evt.seq
