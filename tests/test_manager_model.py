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


def test_model_value_is_stripped_of_surrounding_whitespace():
    m, cap = _mgr(make_spec(args=[], driver="claude"))
    m.start(EXECUTOR, "t", "/tmp", model="  sonnet  ")
    assert cap[0].spec.args == ["--model", "sonnet"]


def test_no_model_leaves_args_byte_identical():
    original = ["--model", "opus", "--foo"]        # even a pre-existing --model is untouched
    spec = make_spec(args=list(original), driver="claude")
    m, cap = _mgr(spec)
    m.start(EXECUTOR, "t", "/tmp")                  # no model kwarg
    assert cap[0].spec.args == original
    assert cap[0].spec is spec                      # SAME spec object: broker argv identical to pre-feature


# ---- shape validation (pass-through: shape only, no allowlist) -------------------------
@pytest.mark.parametrize("bad", ["", "   ", "\t", "\n", "hai\nku", "mo\x00del", "a\x1bb", "x" * 129])
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
