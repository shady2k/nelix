"""nelix-9k0: per-session model override — manager-side validation, capability check, and argv fold.

The fold is asserted against the REAL ExecutorSpec the manager hands the session (spec.argv() is
exactly what the launcher passes the broker), so these exercise the actual spawn-argv assembly.
"""
import io

import pytest

from conftest import EXECUTOR, make_spec
from daemon.events import EventQueue
from daemon.manager import SessionManager, ModelRejected
from daemon.obs import Logger


class _CapSession:
    """Captures the ExecutorSpec the manager builds for this session."""
    def __init__(self, sid, executor, spec):
        self.sid = sid; self.executor = executor; self.spec = spec
        self.on_terminal = None; self.reaper_ctx = None
        self.lineage_id = sid; self.restarted_from = None; self.restart_count = 0
    def start(self, task, cwd): pass
    def snapshot(self): return {"session_id": self.sid, "control_state": "busy",
                                "task_delivery": "pending"}
    def stop(self): pass


def _mgr(spec=None, limit=5, driver_factory=None, logger=None):
    specs = {EXECUTOR: spec or make_spec()}
    captured = []
    def sf(sid, executor, spc, events):
        s = _CapSession(sid, executor, spc); captured.append(s); return s
    m = SessionManager(specs, EventQueue(), session_factory=sf, concurrency_limit=limit,
                       driver_factory=driver_factory, logger=logger)
    return m, captured


# ---- argv fold (last-wins) -------------------------------------------------------------
def test_model_appends_driver_flag_and_value():
    m, cap = _mgr(make_spec(args=["--foo"], driver="claude"))
    m.start(EXECUTOR, "t", "/tmp", model="haiku")
    assert cap[0].spec.args == ["--foo", "--model", "haiku"]
    assert cap[0].spec.argv() == ["x", "--foo", "--model", "haiku"]


def test_last_wins_strips_preexisting_split_form():
    m, cap = _mgr(make_spec(args=["--model", "opus", "--foo"], driver="claude"))
    m.start(EXECUTOR, "t", "/tmp", model="haiku")
    assert cap[0].spec.args == ["--foo", "--model", "haiku"]


def test_last_wins_strips_preexisting_equals_form():
    m, cap = _mgr(make_spec(args=["--model=opus", "--foo"], driver="claude"))
    m.start(EXECUTOR, "t", "/tmp", model="haiku")
    assert cap[0].spec.args == ["--foo", "--model", "haiku"]


def test_duplicate_model_flags_collapse_to_single_injected():
    m, cap = _mgr(make_spec(args=["--model", "a", "--model=b", "--foo"], driver="claude"))
    m.start(EXECUTOR, "t", "/tmp", model="haiku")
    assert cap[0].spec.args == ["--foo", "--model", "haiku"]
    assert cap[0].spec.args.count("--model") == 1


@pytest.mark.parametrize("clean", ["haiku", "claude-fable-5", "GLM-4.7", "us.anthropic.claude-opus"])
def test_clean_model_is_forwarded_verbatim(clean):
    # A clean value is forwarded EXACTLY (no silent normalization) — the CLI is the authority.
    m, cap = _mgr(make_spec(args=[], driver="claude"))
    m.start(EXECUTOR, "t", "/tmp", model=clean)
    assert cap[0].spec.args == ["--model", clean]


def test_no_model_leaves_args_byte_identical():
    original = ["--model", "opus", "--foo"]        # even a pre-existing --model is untouched
    spec = make_spec(args=list(original), driver="claude")
    m, cap = _mgr(spec)
    m.start(EXECUTOR, "t", "/tmp")                  # no model kwarg
    assert cap[0].spec.args == original
    assert cap[0].spec is spec                      # SAME spec object: broker argv identical to pre-feature


# ---- shape validation (pass-through: shape only, no allowlist; verbatim, never normalized) ---------
# Leading/trailing whitespace and ANY ASCII control char (incl edge newline/tab) are REJECTED, not
# silently trimmed — a value that would need normalization to be safe is refused (spec §5).
@pytest.mark.parametrize("bad", [
    "", "   ", "\t", "\n",                 # empty / whitespace-only
    "hai\nku", "mo\x00del", "a\x1bb",      # interior control chars
    "haiku\n", "haiku\t", "\thaiku", "\nhaiku",   # EDGE control chars (were silently trimmed before)
    " haiku", "haiku ", " haiku ",         # leading/trailing space
    "x" * 129])                            # oversized
def test_bad_shape_rejected(bad):
    m, _ = _mgr(make_spec(driver="claude"))
    with pytest.raises(ModelRejected):
        m.start(EXECUTOR, "t", "/tmp", model=bad)


def test_max_length_boundary_accepted():
    m, cap = _mgr(make_spec(args=[], driver="claude"))
    val = "x" * 128
    m.start(EXECUTOR, "t", "/tmp", model=val)
    assert cap[0].spec.args == ["--model", val]


def test_model_rejected_is_a_value_error_subclass():
    assert issubclass(ModelRejected, ValueError)


# ---- driver capability (via getattr, no AttributeError) --------------------------------
class _NoModelDriver:
    """A driver that never declares model_flag (structural typing)."""
    def observe(self, *a, **k): pass


class _NullModelDriver:
    model_flag = None            # declares it, but None -> unsupported


def test_unsupported_driver_missing_flag_rejected():
    m, _ = _mgr(make_spec(driver="claude"), driver_factory=lambda name: _NoModelDriver())
    with pytest.raises(ModelRejected):
        m.start(EXECUTOR, "t", "/tmp", model="haiku")


def test_unsupported_driver_flag_none_rejected():
    m, _ = _mgr(make_spec(driver="claude"), driver_factory=lambda name: _NullModelDriver())
    with pytest.raises(ModelRejected):
        m.start(EXECUTOR, "t", "/tmp", model="haiku")


# ---- placement: validation precedes the concurrency cap (400 not 409) ------------------
def test_bad_shape_rejected_even_at_capacity():
    m, _ = _mgr(make_spec(driver="claude"), limit=1)
    m.start(EXECUTOR, "fill the one slot", "/tmp")     # daemon now at its cap
    with pytest.raises(ModelRejected):                 # NOT RuntimeError(concurrency_limit)
        m.start(EXECUTOR, "t", "/tmp", model="bad\nshape")


def test_unsupported_driver_rejected_even_at_capacity():
    m, _ = _mgr(make_spec(driver="claude"), limit=1,
                driver_factory=lambda name: _NoModelDriver())
    m.start(EXECUTOR, "fill the one slot", "/tmp")
    with pytest.raises(ModelRejected):
        m.start(EXECUTOR, "t", "/tmp", model="haiku")


# ---- override visibility ----------------------------------------------------------------
def _events(buf):
    import json
    return [json.loads(l)["event"] for l in buf.getvalue().splitlines() if l.strip()]


def test_stripping_toml_pinned_model_emits_override_applied():
    buf = io.StringIO()
    m, _ = _mgr(make_spec(args=["--model", "opus"], driver="claude"),
                logger=Logger(level="debug", stream=buf))
    m.start(EXECUTOR, "t", "/tmp", model="haiku")
    assert "model_override_applied" in _events(buf)


def test_no_preexisting_flag_does_not_emit_override_applied():
    buf = io.StringIO()
    m, _ = _mgr(make_spec(args=["--foo"], driver="claude"),
                logger=Logger(level="debug", stream=buf))
    m.start(EXECUTOR, "t", "/tmp", model="haiku")     # nothing pre-existing was overridden
    assert "model_override_applied" not in _events(buf)


# ---- restart / recovery carries the per-session override (FIX 1) ------------------------
# The start-time override is same-lineage RECOVERY state (not runtime switching): an auto-restart
# (crash / delivery-failure) MUST come back on the SAME model, never a silent downgrade to default.
class _RestartCapSession:
    """A restartable session double that captures the ExecutorSpec it was built with and does NOT
    auto-terminate (stays in _sessions so restart() takes the active-session source path)."""
    instances = []
    def __init__(self, sid, executor, spec):
        self._id = sid; self._executor = executor; self.spec = spec
        self.on_terminal = None; self.reaper_ctx = None
        self.lineage_id = None; self.restarted_from = None; self.restart_count = 0
        self.model = None; self._task = None; self._cwd = None; self.stopped = False
        _RestartCapSession.instances.append(self)
    @property
    def executor(self): return self._executor
    @property
    def task(self): return self._task
    @property
    def cwd(self): return self._cwd
    def start(self, task, cwd): self._task = task; self._cwd = cwd
    def stop(self): self.stopped = True
    def snapshot(self): return {"session_id": self._id, "control_state": "busy"}
    def terminal_snapshot(self):
        return {"session_id": self._id, "terminal": True, "terminal_kind": "crashed",
                "lineage_id": self.lineage_id, "restarted_from": self.restarted_from,
                "restart_count": 0}


def _restart_mgr(tmp_path, monkeypatch, spec=None, limit=2):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _RestartCapSession.instances = []
    specs = {EXECUTOR: spec or make_spec(command="claude", args=["--foo"], driver="claude",
                                         max_restarts=3)}
    m = SessionManager(specs, EventQueue(), concurrency_limit=limit,
                       session_factory=lambda sid, ex, sp, ev: _RestartCapSession(sid, ex, sp),
                       session_retain=0, session_max_age_days=0)
    return m


def test_restart_active_session_reinjects_same_model(tmp_path, monkeypatch):
    m = _restart_mgr(tmp_path, monkeypatch)
    out = m.start(EXECUTOR, "task A", str(tmp_path), model="haiku")
    assert _RestartCapSession.instances[0].spec.args == ["--foo", "--model", "haiku"]
    r = m.restart(out.session_id)                          # active-session source path
    assert r.status == "restarted"
    assert _RestartCapSession.instances[1].spec.args == ["--foo", "--model", "haiku"]


def test_restart_from_persisted_meta_reinjects_same_model(tmp_path, monkeypatch):
    import paths
    m = _restart_mgr(tmp_path, monkeypatch)
    out = m.start(EXECUTOR, "task B", str(tmp_path), model="sonnet"); sid = out.session_id
    # Simulate a crash: persist meta WITH the model (real _write_meta does this) and free the slot.
    paths.ensure_private_dir(paths.sessions_root() / sid)
    paths.session_meta(paths.sessions_root() / sid).write_text(
        __import__("json").dumps({"executor": EXECUTOR, "task": "task B", "cwd": str(tmp_path),
                                  "lineage_id": sid, "restarted_from": None, "model": "sonnet"}))
    m._free_slot(sid)                                      # gone from _sessions -> meta source path
    r = m.restart(sid)
    assert r.status == "restarted"
    assert _RestartCapSession.instances[-1].spec.args == ["--foo", "--model", "sonnet"]


def test_restart_without_model_argv_matches_no_model_baseline(tmp_path, monkeypatch):
    m = _restart_mgr(tmp_path, monkeypatch)
    out = m.start(EXECUTOR, "task C", str(tmp_path))       # NO model
    assert _RestartCapSession.instances[0].spec.args == ["--foo"]
    r = m.restart(out.session_id)
    assert r.status == "restarted"
    assert _RestartCapSession.instances[1].spec.args == ["--foo"]     # byte-identical, no --model


def test_restart_from_old_meta_without_model_key_is_clean(tmp_path, monkeypatch):
    import paths
    m = _restart_mgr(tmp_path, monkeypatch)
    out = m.start(EXECUTOR, "task D", str(tmp_path)); sid = out.session_id
    # OLD meta shape: no "model" key at all -> restart must default to None (no override, no crash).
    paths.ensure_private_dir(paths.sessions_root() / sid)
    paths.session_meta(paths.sessions_root() / sid).write_text(
        __import__("json").dumps({"executor": EXECUTOR, "task": "task D", "cwd": str(tmp_path),
                                  "lineage_id": sid, "restarted_from": None}))
    m._free_slot(sid)
    r = m.restart(sid)
    assert r.status == "restarted"
    assert _RestartCapSession.instances[-1].spec.args == ["--foo"]    # no override
