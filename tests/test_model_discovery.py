import io, json, urllib.error
import pytest
import daemon.model_discovery as md


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def _install_opener(monkeypatch, *, body=None, exc=None, capture=None):
    def fake_open(req, timeout=None):
        if capture is not None:
            capture["url"] = req.full_url
            capture["headers"] = {k.lower(): v for k, v in req.header_items()}
        if exc is not None:
            raise exc
        return _Resp(body)
    monkeypatch.setattr(md, "_open", fake_open)


def test_parses_data_into_id_displayname(monkeypatch):
    body = json.dumps({"data": [{"id": "glm-4.6", "display_name": "GLM-4.6"},
                                 {"id": "glm-5.2", "display_name": "GLM-5.2"}]}).encode()
    _install_opener(monkeypatch, body=body)
    out = md.discover("anthropic", {"ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"})
    assert out == [{"id": "glm-4.6", "display_name": "GLM-4.6"},
                   {"id": "glm-5.2", "display_name": "GLM-5.2"}]


def test_bearer_vs_apikey_and_url(monkeypatch):
    cap = {}
    _install_opener(monkeypatch, body=json.dumps({"data": []}).encode(), capture=cap)
    md.discover("anthropic", {"ANTHROPIC_AUTH_TOKEN": "secret", "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic/"})
    assert cap["url"] == "https://api.z.ai/api/anthropic/v1/models?limit=1000"   # trailing slash trimmed
    assert cap["headers"]["authorization"] == "Bearer secret"
    cap.clear()
    _install_opener(monkeypatch, body=json.dumps({"data": []}).encode(), capture=cap)
    md.discover("anthropic", {"ANTHROPIC_API_KEY": "k"})   # no base_url -> default host
    assert cap["url"] == "https://api.anthropic.com/v1/models?limit=1000"
    assert cap["headers"]["x-api-key"] == "k"
    assert cap["headers"]["anthropic-version"] == "2023-06-01"


def test_no_auth_raises_auth_missing(monkeypatch):
    _install_opener(monkeypatch, body=b"{}")
    with pytest.raises(md.DiscoveryAuthMissing):
        md.discover("anthropic", {})


def test_200_without_data_is_discovery_error(monkeypatch):     # z.ai 200-on-auth-fail
    _install_opener(monkeypatch, body=json.dumps({"success": False, "code": 1001}).encode())
    with pytest.raises(md.DiscoveryError) as e:
        md.discover("anthropic", {"ANTHROPIC_AUTH_TOKEN": "t"})
    assert e.value.reason == "bad_shape"


def test_http_error_is_redacted(monkeypatch):
    _install_opener(monkeypatch, exc=urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))
    with pytest.raises(md.DiscoveryError) as e:
        md.discover("anthropic", {"ANTHROPIC_AUTH_TOKEN": "supersecret"})
    assert e.value.reason == "http_error"
    assert "supersecret" not in str(e.value) and "401" not in str(e.value)


def test_unsupported_protocol(monkeypatch):
    with pytest.raises(md.DiscoveryError) as e:
        md.discover("openai", {"ANTHROPIC_AUTH_TOKEN": "t"})
    assert e.value.reason == "unsupported_protocol"


def test_no_redirect_opener_rejects_3xx():
    # The opener must NOT follow redirects (token-leak guard): redirect_request returns None.
    h = md._NoRedirect()
    assert h.redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None


def test_caps_model_count_and_id_length(monkeypatch):
    data = [{"id": "m" * 500, "display_name": "d" * 500}] + [{"id": f"m{i}"} for i in range(1000)]
    _install_opener(monkeypatch, body=json.dumps({"data": data}).encode())
    out = md.discover("anthropic", {"ANTHROPIC_AUTH_TOKEN": "t"})
    assert len(out) <= md._MAX_MODELS
    assert len(out[0]["id"]) <= md._MAX_ID_LEN
