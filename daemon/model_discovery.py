"""nelix-kwr: driver-keyed model discovery. Pure + stateless: given an executor's resolved env,
GET the backend's /v1/models and return a normalized list. HTTP lives here ONLY (never in the driver
class or the core). Leak-safe: the token is used as a header and never logged/stored/returned; every
failure is a DiscoveryError carrying an enum reason only (no url/status/body/headers/exception text)."""
import json
import urllib.error
import urllib.request

_MODELS_MAX_BYTES = 65536     # read cap before JSON parse (memory hygiene)
_MAX_MODELS = 500             # cap the returned list
_MAX_ID_LEN = 200             # cap id/display_name length
_DISCOVERY_TIMEOUT = 5.0      # per-call HTTP deadline (seconds)
_ANTHROPIC_VERSION = "2023-06-01"


class DiscoveryError(Exception):
    """Model discovery failed. `reason` is enum-like; nothing else crosses this boundary."""
    def __init__(self, reason):
        super().__init__(f"model discovery failed: {reason}")
        self.reason = reason


class DiscoveryAuthMissing(DiscoveryError):
    """No auth credential found in the resolved env -> discovery cannot run (fail-open upstream)."""
    def __init__(self):
        super().__init__("no_auth")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Returning None -> urllib does NOT follow the redirect and raises HTTPError instead, so the
    # Authorization/x-api-key header can never be replayed to a redirect target (token-leak guard).
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(req, timeout):        # seam for tests to patch
    return _OPENER.open(req, timeout=timeout)


def auth_of(env):
    """(kind, token): ('bearer', <ANTHROPIC_AUTH_TOKEN>) | ('apikey', <ANTHROPIC_API_KEY>) | (None, None)."""
    tok = env.get("ANTHROPIC_AUTH_TOKEN")
    if tok:
        return "bearer", tok
    key = env.get("ANTHROPIC_API_KEY")
    if key:
        return "apikey", key
    return None, None


def _discover_anthropic(env):
    kind, token = auth_of(env)
    if kind is None:
        raise DiscoveryAuthMissing()
    base = (env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
    headers = {}
    if kind == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    else:
        headers["x-api-key"] = token
        headers["anthropic-version"] = _ANTHROPIC_VERSION
    req = urllib.request.Request(f"{base}/v1/models?limit=1000", headers=headers, method="GET")
    try:
        with _open(req, _DISCOVERY_TIMEOUT) as resp:
            raw = resp.read(_MODELS_MAX_BYTES + 1)
    except (urllib.error.URLError, OSError):
        raise DiscoveryError("http_error") from None      # redacted: no status/url/body
    if len(raw) > _MODELS_MAX_BYTES:
        raise DiscoveryError("too_large")
    try:
        doc = json.loads(raw)
    except ValueError:
        raise DiscoveryError("bad_json") from None
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, list):
        raise DiscoveryError("bad_shape")                  # z.ai 200-on-auth-fail lands here
    out = []
    for m in data[:_MAX_MODELS]:
        if isinstance(m, dict) and isinstance(m.get("id"), str):
            dn = m.get("display_name")
            out.append({"id": m["id"][:_MAX_ID_LEN],
                        "display_name": (dn[:_MAX_ID_LEN] if isinstance(dn, str) else None)})
    return out


_STRATEGIES = {"anthropic": _discover_anthropic}


def discover(protocol, env):
    fn = _STRATEGIES.get(protocol)
    if fn is None:
        raise DiscoveryError("unsupported_protocol")
    return fn(env)
