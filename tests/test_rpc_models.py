"""nelix-g9k: GET /models — the read-only model-discovery route. It maps manager.models outcomes
to distinct HTTP codes IN-BRANCH (200/404/400/502), NEVER the generic 500/exc_info path, and emits
a dedicated REDACTED log record (event "models" + executor + reason — no command/stdout/output).
"""
import io
import json
import threading
import urllib.error
import urllib.request

from daemon.env_resolver import EnvResolveError
from daemon.events import EventQueue
from daemon.manager import ModelsCmdError, ModelsNotConfigured
from daemon.obs import Logger
from daemon.rpc_server import make_server
from daemon.transport import Transport

_PORT = [8830]


def _next_port():
    _PORT[0] += 1
    return _PORT[0]


class ModelsManager:
    """Minimal manager exposing only what /models touches: `_events` (referenced by make_server's
    other routes, not by /models) and `models()`, whose behaviour each test injects."""

    def __init__(self, behavior):
        self._events = EventQueue()
        self._behavior = behavior          # callable(executor) -> (text, truncated) | raises

    def models(self, executor):
        return self._behavior(executor)


def _serve(behavior, logger=None):
    port = _next_port()
    srv = make_server(ModelsManager(behavior), Transport.tcp("127.0.0.1", port, "t"), logger=logger)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def _get(port, path, token="t"):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET",
                               headers={"X-Nelix-Token": token})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_models_ok_returns_200_output_and_truncated():
    srv, port = _serve(lambda ex: ("model-a\nmodel-b (Display)", False))
    try:
        st, body = _get(port, "/models?executor=demo")
        assert st == 200
        assert body == {"output": "model-a\nmodel-b (Display)", "truncated": False}
    finally:
        srv.shutdown()


def test_models_unknown_executor_returns_404():
    def boom(ex):
        raise ValueError(f"unknown executor: {ex!r}")
    srv, port = _serve(boom)
    try:
        st, body = _get(port, "/models?executor=nope")
        assert st == 404 and "error" in body
    finally:
        srv.shutdown()


def test_models_not_configured_returns_400():
    def boom(ex):
        raise ModelsNotConfigured(ex)
    srv, port = _serve(boom)
    try:
        st, body = _get(port, "/models?executor=demo")
        assert st == 400 and "error" in body
    finally:
        srv.shutdown()


def test_models_cmd_error_returns_502_redacted():
    def boom(ex):
        raise ModelsCmdError("non_zero_exit")
    srv, port = _serve(boom)
    try:
        st, body = _get(port, "/models?executor=demo")
        assert st == 502
        assert body["error"] == {"executor": "demo", "reason": "non_zero_exit"}
    finally:
        srv.shutdown()


def test_models_env_resolve_error_returns_502_redacted():
    def boom(ex):
        raise EnvResolveError("TOK", "timeout")
    srv, port = _serve(boom)
    try:
        st, body = _get(port, "/models?executor=demo")
        assert st == 502
        # Redacted to {executor, reason} only — the env var name is NOT leaked into the wire body.
        assert body["error"] == {"executor": "demo", "reason": "timeout"}
    finally:
        srv.shutdown()


def test_models_missing_executor_param_is_400():
    srv, port = _serve(lambda ex: ("x", False))
    try:
        st, body = _get(port, "/models")
        assert st == 400 and "error" in body
    finally:
        srv.shutdown()


def test_models_errors_never_hit_the_generic_500_path():
    # Each mapped error must be caught in-branch; none may fall through to do_GET's Exception->500.
    for behavior in (lambda ex: (_ for _ in ()).throw(ValueError("unknown")),
                     lambda ex: (_ for _ in ()).throw(ModelsNotConfigured(ex)),
                     lambda ex: (_ for _ in ()).throw(ModelsCmdError("empty_output")),
                     lambda ex: (_ for _ in ()).throw(EnvResolveError("V", "spawn_failed"))):
        srv, port = _serve(behavior)
        try:
            st, _ = _get(port, "/models?executor=demo")
            assert st != 500
        finally:
            srv.shutdown()


def test_models_log_record_is_redacted_on_success_and_failure():
    # The route logs a dedicated record (event "models" + executor + reason), NEVER the command,
    # stdout, or the relayed output. Success path:
    buf = io.StringIO()
    logger = Logger(level="debug", stream=buf)
    srv, port = _serve(lambda ex: ("SECRET_MODEL_LIST_OUTPUT", False), logger=logger)
    try:
        _get(port, "/models?executor=demo")
    finally:
        srv.shutdown()
    recs = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
    models_recs = [r for r in recs if r.get("event") == "models"]
    assert models_recs, "a 'models' record must be emitted"
    assert models_recs[0]["executor"] == "demo"
    assert models_recs[0]["reason"] == "ok"
    assert "SECRET_MODEL_LIST_OUTPUT" not in buf.getvalue()   # the relayed output is never logged

    # Failure path: the record carries the reason but no command/stdout/stderr.
    buf2 = io.StringIO()
    logger2 = Logger(level="debug", stream=buf2)
    srv2, port2 = _serve(lambda ex: (_ for _ in ()).throw(ModelsCmdError("non_zero_exit")),
                         logger=logger2)
    try:
        _get(port2, "/models?executor=demo")
    finally:
        srv2.shutdown()
    recs2 = [json.loads(l) for l in buf2.getvalue().splitlines() if l.strip()]
    m2 = [r for r in recs2 if r.get("event") == "models"]
    assert m2 and m2[0]["reason"] == "non_zero_exit"
    assert "/bin/sh" not in buf2.getvalue()
