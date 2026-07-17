"""nelix-9a4.6 deliverable C: per-session capabilities (spec §8) — "An operation unavailable on an
older session needs a stable `unsupported_by_generation` response OR per-session capabilities. A
single global capabilities response from N is insufficient for operations targeting N-1."

There is only ONE generation today, so the cross-generation case is untested (deliberately, per
the brief's resolution of deliverable D — building a server-side gate for it would be dead code).
What IS real and per-session TODAY is the driver/launcher pair a session was built with
(daemon/drivers/base.py `hook_capable`, daemon/launchers/base.py `ExecutorCapabilities`) — these
tests exercise that real axis of variation, which is exactly what makes `unsupported_by_generation`
reachable+tested per the brief: the capabilities payload names it as the code a caller would get
for the one caller-facing operation (`message`) that a hookless driver's session cannot usefully
serve.
"""
from conftest import EXECUTOR, OWNER, make_spec, own
from daemon.events import EventQueue
from daemon.launchers.base import ExecutorCapabilities
from daemon.manager import SessionManager


class _FakeDriver:
    def __init__(self, hook_capable):
        self.hook_capable = hook_capable


class _FakeLauncher:
    def __init__(self, capabilities):
        self.capabilities = capabilities


class _FakeSession:
    def __init__(self, sid, executor, driver, launcher):
        self.sid = sid
        self.executor = executor
        self._driver = driver
        self._launcher = launcher

    def start(self, task, cwd):
        pass

    def snapshot(self):
        return {"session_id": self.sid, "executor": self.executor,
                "control_state": "busy", "task_delivery": "pending"}

    def stop(self):
        pass


def _mgr(driver, launcher, limit=5):
    specs = {EXECUTOR: make_spec()}
    q = EventQueue()

    def session_factory(sid, executor, spec, events):
        return _FakeSession(sid, executor, driver, launcher)

    return SessionManager(specs, q, session_factory=session_factory, concurrency_limit=limit)


def test_capabilities_unknown_session_is_none():
    m = _mgr(_FakeDriver(True), _FakeLauncher(ExecutorCapabilities(isolation_class="host")))
    assert m.capabilities("s-nope", owner_id=OWNER) is None


def test_capabilities_foreign_owner_is_none():
    m = _mgr(_FakeDriver(True), _FakeLauncher(ExecutorCapabilities(isolation_class="host")))
    out = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER)
    assert m.capabilities(out.session_id, owner_id="someone-else") is None


def test_capabilities_hook_capable_session_reports_all_operations_supported():
    m = _mgr(_FakeDriver(True),
             _FakeLauncher(ExecutorCapabilities(isolation_class="host", can_attach=False)))
    out = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER)
    caps = m.capabilities(out.session_id, owner_id=OWNER)
    assert caps["session_id"] == out.session_id
    assert caps["executor"] == EXECUTOR
    assert caps["hook_capable"] is True
    assert caps["isolation_class"] == "host"
    assert caps["can_attach"] is False
    for op in ("respond", "stop", "restart", "message", "dialog", "screen"):
        assert caps["operations"][op]["supported"] is True, op


def test_capabilities_hookless_session_message_is_unsupported_by_generation():
    # THE reachability case for D: a driver with hook_capable=False cannot usefully serve the
    # message plane (nelix-question/nelix-note are only injected for a hook-capable driver's
    # session — daemon/launchers/local.py `_driver_hook_capable`), so the capabilities response
    # names the stable code a caller calling /message on this session would need to know about.
    m = _mgr(_FakeDriver(False),
             _FakeLauncher(ExecutorCapabilities(isolation_class="host", can_attach=False)))
    out = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER)
    caps = m.capabilities(out.session_id, owner_id=OWNER)
    assert caps["hook_capable"] is False
    assert caps["operations"]["message"] == {"supported": False, "code": "unsupported_by_generation"}
    # Every OTHER operation is unaffected by hook_capable.
    for op in ("respond", "stop", "restart", "dialog", "screen"):
        assert caps["operations"][op]["supported"] is True, op


def test_capabilities_reflects_launcher_isolation_and_attach_facts():
    m = _mgr(_FakeDriver(True),
             _FakeLauncher(ExecutorCapabilities(isolation_class="container", can_attach=True)))
    out = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER)
    caps = m.capabilities(out.session_id, owner_id=OWNER)
    assert caps["isolation_class"] == "container"
    assert caps["can_attach"] is True


def test_capabilities_baseline_without_session_id_lists_known_executors():
    m = _mgr(_FakeDriver(True), _FakeLauncher(ExecutorCapabilities(isolation_class="host")))
    baseline = m.capabilities(owner_id=OWNER)          # no session_id -> generation-level baseline
    assert "executors" in baseline
    assert EXECUTOR in baseline["executors"]
    entry = baseline["executors"][EXECUTOR]
    assert entry["driver"] == "claude" and entry["launcher"] == "local"
    # Baseline uses the REAL registered claude/local classes (not the fake session's driver),
    # since it describes the CONFIGURED executor, not any particular live session.
    assert "hook_capable" in entry and "isolation_class" in entry and "can_attach" in entry


def test_capabilities_baseline_works_with_no_sessions_started():
    # The generation-level baseline is not session-scoped: it must answer before anything starts.
    m = _mgr(_FakeDriver(True), _FakeLauncher(ExecutorCapabilities(isolation_class="host")))
    baseline = m.capabilities(owner_id=OWNER)
    assert EXECUTOR in baseline["executors"]
