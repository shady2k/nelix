import pytest
from dataclasses import replace
from daemon.manager import SessionManager, ModelUnavailable
from daemon.env_resolver import EnvResolveError


class _Driver:
    model_flag = "--model"
    models_protocol = "anthropic"
    model_aliases = frozenset({"haiku", "sonnet", "opus"})


def _mgr(monkeypatch, spec, *, discovered=None, disc_exc=None, driver=None):
    m = SessionManager.__new__(SessionManager)   # bypass full __init__; wire only what the check needs
    m._logger = None
    m._specs = {"zai": spec}
    m._driver_factory = lambda name: (driver or _Driver())
    def fake_resolve(env_cmd, base, timeout, logger=None):
        return {}
    monkeypatch.setattr("daemon.manager.resolve_env_cmds", fake_resolve)
    def fake_discover(protocol, env):
        if disc_exc is not None:
            raise disc_exc
        return discovered or []
    from daemon.model_cache import ModelCache
    m._model_cache = ModelCache(fake_discover, clock=lambda: 0.0)
    return m


class _Spec:
    driver = "claude"
    env_cmd = {}
    env_cmd_timeout_seconds = 15.0
    def resolved_env(self):
        return {"ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"}


def test_present_model_passes(monkeypatch):
    m = _mgr(monkeypatch, _Spec(), discovered=[{"id": "glm-5.2", "display_name": "GLM-5.2"}])
    m._check_model_available(_Spec(), "zai", "glm-5.2")        # no raise


def test_case_insensitive_match(monkeypatch):
    m = _mgr(monkeypatch, _Spec(), discovered=[{"id": "glm-5.2", "display_name": "GLM-5.2"}])
    m._check_model_available(_Spec(), "zai", "GLM-5.2")        # no raise


def test_absent_model_raises_with_list(monkeypatch):
    models = [{"id": "glm-5.2", "display_name": "GLM-5.2"}]
    m = _mgr(monkeypatch, _Spec(), discovered=models)
    with pytest.raises(ModelUnavailable) as e:
        m._check_model_available(_Spec(), "zai", "glm-9.9")
    assert e.value.available_models == models


def test_alias_passes_without_discovery(monkeypatch):
    m = _mgr(monkeypatch, _Spec(), disc_exc=AssertionError("must not fetch"))
    m._check_model_available(_Spec(), "zai", "opus")          # alias -> skip, no raise


def test_no_protocol_skips(monkeypatch):
    class D: model_flag = "--model"                            # no models_protocol
    m = _mgr(monkeypatch, _Spec(), disc_exc=AssertionError("must not fetch"), driver=D())
    m._check_model_available(_Spec(), "zai", "anything")      # skip


def test_no_token_fails_open(monkeypatch):
    class _NoTok(_Spec):
        def resolved_env(self): return {}                     # OAuth-style: no token
    m = _mgr(monkeypatch, _NoTok(), disc_exc=AssertionError("must not fetch"))
    m._check_model_available(_NoTok(), "zai", "glm-9.9")      # fail-open -> no raise


def test_discovery_error_fails_open(monkeypatch):
    from daemon.model_discovery import DiscoveryError
    m = _mgr(monkeypatch, _Spec(), disc_exc=DiscoveryError("http_error"))
    m._check_model_available(_Spec(), "zai", "glm-9.9")      # fail-open -> no raise


def test_env_resolve_error_propagates(monkeypatch):
    m = _mgr(monkeypatch, _Spec())
    monkeypatch.setattr("daemon.manager.resolve_env_cmds",
                        lambda *a, **k: (_ for _ in ()).throw(EnvResolveError("V", "timeout")))
    with pytest.raises(EnvResolveError):
        m._check_model_available(_Spec(), "zai", "glm-9.9")
