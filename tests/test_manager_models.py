"""nelix-g9k: SessionManager.models runs the executor's models_cmd with its RESOLVED env and
relays stdout. Drives the REAL subprocess path (real specs, real /bin/sh) — no fabricated frames.
Fail-closed + no-leak are asserted against a real obs.Logger.
"""
import io

import pytest

from conftest import EXECUTOR, make_spec
from daemon.env_resolver import EnvResolveError
from daemon.events import EventQueue
from daemon.manager import ModelsCmdError, ModelsNotConfigured, SessionManager
from daemon.obs import Logger


def _mgr(spec, buf=None, executor=EXECUTOR):
    logger = Logger(level="debug", stream=buf) if buf is not None else None
    return SessionManager({executor: spec}, EventQueue(), logger=logger)


def test_models_relays_stdout_verbatim():
    spec = make_spec(command="claude", driver="claude",
                     models_cmd="printf 'model-a\\nmodel-b (Display)\\n'")
    text, truncated = _mgr(spec).models(EXECUTOR)
    assert text == "model-a\nmodel-b (Display)"     # trailing newline stripped, interior preserved
    assert truncated is False


def test_models_sees_resolved_env_cmd_value():
    # The resolved env (incl a c5o env_cmd value) is visible to models_cmd — a command echoing a
    # resolved var proves models_cmd runs with the same env the child would get at spawn.
    spec = make_spec(command="claude", driver="claude",
                     env_cmd={"MODELS_TOKEN": "echo resolved-secret"},
                     models_cmd='printf %s "$MODELS_TOKEN"')
    text, _ = _mgr(spec).models(EXECUTOR)
    assert text == "resolved-secret"


def test_models_sees_static_env_value():
    spec = make_spec(command="claude", driver="claude", env={"SERVICE_BASE_URL": "https://x"},
                     models_cmd='printf %s "$SERVICE_BASE_URL"')
    text, _ = _mgr(spec).models(EXECUTOR)
    assert text == "https://x"


def test_models_unknown_executor_raises_value_error():
    spec = make_spec(command="claude", driver="claude", models_cmd="echo x")
    with pytest.raises(ValueError):
        _mgr(spec).models("nope")


def test_models_not_configured_raises():
    spec = make_spec(command="claude", driver="claude")          # no models_cmd
    with pytest.raises(ModelsNotConfigured) as ei:
        _mgr(spec).models(EXECUTOR)
    assert ei.value.executor == EXECUTOR


def test_models_non_zero_exit_is_models_cmd_error():
    spec = make_spec(command="claude", driver="claude", models_cmd="echo out; exit 4")
    with pytest.raises(ModelsCmdError) as ei:
        _mgr(spec).models(EXECUTOR)
    assert ei.value.reason == "non_zero_exit"


def test_models_empty_output_is_models_cmd_error():
    spec = make_spec(command="claude", driver="claude", models_cmd="true")
    with pytest.raises(ModelsCmdError) as ei:
        _mgr(spec).models(EXECUTOR)
    assert ei.value.reason == "empty_output"


def test_models_timeout_is_models_cmd_error():
    spec = make_spec(command="claude", driver="claude", models_cmd="sleep 5",
                     models_cmd_timeout_seconds=0.2)
    with pytest.raises(ModelsCmdError) as ei:
        _mgr(spec).models(EXECUTOR)
    assert ei.value.reason == "timeout"


def test_models_env_cmd_failure_surfaces_as_env_resolve_error():
    # A failing env_cmd (resolved BEFORE models_cmd runs) surfaces as EnvResolveError, NOT a
    # ModelsCmdError — the route maps both to 502 but the attribution stays honest (env_cmd was
    # the failure, models_cmd never ran).
    spec = make_spec(command="claude", driver="claude",
                     env_cmd={"TOK": "exit 7"}, models_cmd="echo never")
    with pytest.raises(EnvResolveError) as ei:
        _mgr(spec).models(EXECUTOR)
    assert ei.value.var == "TOK" and ei.value.reason == "non_zero_exit"


def test_models_cmd_error_carries_only_reason_no_leak():
    # ModelsCmdError carries only the reason — never the command, stdout, or stderr.
    spec = make_spec(command="claude", driver="claude",
                     models_cmd="echo LEAKOUT_MODELS; echo LEAKERR_MODELS 1>&2; exit 5")
    with pytest.raises(ModelsCmdError) as ei:
        _mgr(spec).models(EXECUTOR)
    e = ei.value
    assert e.reason == "non_zero_exit"
    assert not hasattr(e, "command") and not hasattr(e, "stdout") and not hasattr(e, "stderr")
    assert "LEAKOUT" not in str(e) and "LEAKERR" not in str(e)


def test_models_no_leak_in_logger_on_success_and_failure():
    # With a real Logger wired into the manager, neither the command, its stdout, nor its stderr
    # appears in ANY log record — on the success path OR the failure path (spec §5, §7 no-leak).
    buf = io.StringIO()
    ok_spec = make_spec(command="claude", driver="claude",
                        models_cmd="echo SECRET_MODEL_LIST_MARKER")
    _mgr(ok_spec, buf).models(EXECUTOR)
    fail_spec = make_spec(command="claude", driver="claude",
                          models_cmd="echo SECRET_FAIL_STDOUT 1>&2; exit 3")
    with pytest.raises(ModelsCmdError):
        _mgr(fail_spec, buf).models(EXECUTOR)
    out = buf.getvalue()
    assert "SECRET_MODEL_LIST_MARKER" not in out
    assert "SECRET_FAIL_STDOUT" not in out
    assert "/bin/sh" not in out


def test_models_does_not_hold_lock_across_subprocess():
    # LOCKLESS: models() must never hold self._lock across the subprocess. A sentinel lock whose
    # acquire() records depth proves the subprocess runs with the lock released (a held lock would
    # serialize model reads behind every session operation).
    spec = make_spec(command="claude", driver="claude", models_cmd="echo ok")
    m = _mgr(spec)
    held_during = {"any": False}
    real_lock = m._lock

    class _Probe:
        def __enter__(self):
            held_during["any"] = True
            return real_lock.__enter__()
        def __exit__(self, *a):
            return real_lock.__exit__(*a)
    m._lock = _Probe()
    m.models(EXECUTOR)
    assert held_during["any"] is False        # models() acquired the lock ZERO times
