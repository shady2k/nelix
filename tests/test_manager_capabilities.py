"""nelix-9a4.6 deliverable C: per-session capabilities (spec §8) — "An operation unavailable on an
older session needs a stable `unsupported_by_generation` response OR per-session capabilities. A
single global capabilities response from N is insufficient for operations targeting N-1."

There is only ONE generation today, so the cross-generation case is untested (deliberately). Fix
pass (review): the original implementation ALSO emitted a per-operation `operations` map, coding
`message` as `unsupported_by_generation` whenever the driver was not `hook_capable`. Both reviewers
confirmed that code was FABRICATED — §8's `unsupported_by_generation` names a cross-GENERATION
incompatibility, not a per-driver one, and `/message` does not actually gate on `hook_capable` at
all, so the payload advertised a failure the operation could never return. That map is REMOVED; the
per-session response is now just the real, truthful FACTS (executor, hook_capable, isolation_class,
can_attach) — what IS real and per-session TODAY is the driver/launcher pair a session was built
with (daemon/drivers/base.py `hook_capable`, daemon/launchers/base.py `ExecutorCapabilities`), and
these tests exercise that real axis of variation as FACTS, not as a fabricated operation-support
code. A genuine `unsupported_by_generation` RESPONSE is deferred to Plan 4 (multi-generation
lifecycle), where more than one generation coexisting makes the cross-generation case real.
"""
from nelix_store.store import Store
from nelix_store.ledger import StartLedger

from conftest import EXECUTOR, OWNER, make_spec, own, reserve_start
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


def _mgr(driver, launcher, tmp_path, limit=5):
    root = tmp_path / "nelix-db"
    root.mkdir()
    store = Store(root)
    ledger = StartLedger(root)
    specs = {EXECUTOR: make_spec()}
    q = EventQueue()

    def session_factory(sid, executor, spec, events):
        return _FakeSession(sid, executor, driver, launcher)

    mgr = SessionManager(specs, q, store, session_factory=session_factory,
                         concurrency_limit=limit)
    return mgr, ledger


def test_capabilities_unknown_session_is_none(tmp_path):
    m, _ledger = _mgr(_FakeDriver(True), _FakeLauncher(ExecutorCapabilities(isolation_class="host")),
                      tmp_path)
    assert m.capabilities("s-nope", owner_id=OWNER) is None


def test_capabilities_foreign_owner_is_none(tmp_path):
    m, ledger = _mgr(_FakeDriver(True), _FakeLauncher(ExecutorCapabilities(isolation_class="host")),
                     tmp_path)
    out = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER,
                  session_id=reserve_start(ledger))
    assert m.capabilities(out.session_id, owner_id="someone-else") is None


def test_capabilities_hook_capable_session_reports_the_facts(tmp_path):
    m, ledger = _mgr(_FakeDriver(True),
                     _FakeLauncher(ExecutorCapabilities(isolation_class="host", can_attach=False)),
                     tmp_path)
    out = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER,
                  session_id=reserve_start(ledger))
    caps = m.capabilities(out.session_id, owner_id=OWNER)
    assert caps == {
        "session_id": out.session_id,
        "executor": EXECUTOR,
        "hook_capable": True,
        "isolation_class": "host",
        "can_attach": False,
    }


def test_capabilities_hookless_session_reports_hook_capable_false_as_a_fact(tmp_path):
    # Fix pass (review): this used to assert a fabricated `operations["message"]` entry coded
    # `unsupported_by_generation`. That code name is a spec §8 CROSS-GENERATION concept and
    # /message never actually gated on hook_capable, so the entry was fictional. What IS real: a
    # driver with hook_capable=False cannot usefully serve the message plane (nelix-question/
    # nelix-note are only injected for a hook-capable driver's session — daemon/launchers/local.py
    # `_driver_hook_capable`) — the response reports that as a plain FACT, no operation-support
    # code attached.
    m, ledger = _mgr(_FakeDriver(False),
                     _FakeLauncher(ExecutorCapabilities(isolation_class="host", can_attach=False)),
                     tmp_path)
    out = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER,
                  session_id=reserve_start(ledger))
    caps = m.capabilities(out.session_id, owner_id=OWNER)
    assert caps == {
        "session_id": out.session_id,
        "executor": EXECUTOR,
        "hook_capable": False,
        "isolation_class": "host",
        "can_attach": False,
    }
    assert "operations" not in caps


def test_capabilities_reflects_launcher_isolation_and_attach_facts(tmp_path):
    m, ledger = _mgr(_FakeDriver(True),
                     _FakeLauncher(ExecutorCapabilities(isolation_class="container", can_attach=True)),
                     tmp_path)
    out = m.start(EXECUTOR, "t", "/tmp", owner_id=OWNER,
                  session_id=reserve_start(ledger))
    caps = m.capabilities(out.session_id, owner_id=OWNER)
    assert caps["isolation_class"] == "container"
    assert caps["can_attach"] is True


def test_capabilities_baseline_without_session_id_lists_known_executors(tmp_path):
    m, _ledger = _mgr(_FakeDriver(True), _FakeLauncher(ExecutorCapabilities(isolation_class="host")),
                      tmp_path)
    baseline = m.capabilities(owner_id=OWNER)          # no session_id -> generation-level baseline
    assert "executors" in baseline
    assert EXECUTOR in baseline["executors"]
    entry = baseline["executors"][EXECUTOR]
    assert entry["driver"] == "claude" and entry["launcher"] == "local"
    # Baseline uses the REAL registered claude/local classes (not the fake session's driver),
    # since it describes the CONFIGURED executor, not any particular live session.
    assert "hook_capable" in entry and "isolation_class" in entry and "can_attach" in entry


def test_capabilities_baseline_works_with_no_sessions_started(tmp_path):
    # The generation-level baseline is not session-scoped: it must answer before anything starts.
    m, _ledger = _mgr(_FakeDriver(True), _FakeLauncher(ExecutorCapabilities(isolation_class="host")),
                      tmp_path)
    baseline = m.capabilities(owner_id=OWNER)
    assert EXECUTOR in baseline["executors"]
