import json
import re
import sys
import time
import traceback as _traceback

# Mask: key=value where value looks secret-ish, and standalone long tokens.
_KV = re.compile(r"((?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*)(\S+)", re.I)
# Standalone opaque token: a 32+ char run of letters/digits. The floor (32) sits
# above the longest real words/compound identifiers (kebab/snake split on `-`/`_`
# into short sub-tokens like `acmetool`+`redirector`), so benign identifiers in
# free text survive, while bare opaque secrets (32+ hex/base64, no prefix, no
# key=) are still caught. Prefixed secrets (sk-/ghp_/AKIA/eyJ) and key=value are
# handled by _KV/_PREFIXED/_BEARER regardless of length.
_LONGTOK = re.compile(r"\b[A-Za-z0-9]{32,}\b")
# Separator-tolerant opaque token (nelix-4ei follow-up): base64url / session /
# bearer tokens that contain `-` or `_` never form a single 32+ sub-run between
# separators, so _LONGTOK misses them. We match any 32+ run in [A-Za-z0-9_-] and
# mask it ONLY when it spans all three of digit/upper/lower — the signature of an
# opaque token. Benign kebab/snake identifiers (short, single-case, no digit) and
# hyphenated UUIDs (single-case hex, 36 chars) don't span three classes, so they
# survive. Applied AFTER _KV/_PREFIXED/_BEARER (see redact()) so a dotted prefixed
# secret is masked whole first; then BEFORE _LONGTOK so a bare separator-bearing
# token still masks as one ***.
_LONGTOK_SEP = re.compile(r"[A-Za-z0-9_-]{32,}")
# Mask: known secret prefixes (Bearer, sk-, ghp_, gho_, ghs_, xox, AKIA, ASIA, eyJ)
# regardless of length. ASIA = AWS temporary (STS/S3) access-key IDs; AKIA = long-term.
_PREFIXED = re.compile(r"\b(?:sk-|ghp_|gho_|ghs_|xox[a-z]-|AKIA|ASIA|eyJ)[A-Za-z0-9._\-]+")
# Mask: Bearer <token> form
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}

# Field names whose value is masked wholesale (compared with _ and - stripped, lowercased).
_SECRET_FIELDS = {
    "env", "token", "authtoken", "accesstoken", "refreshtoken", "authorization",
    "password", "secret", "clientsecret", "apikey", "privatekey",
    "credential", "credentials", "cookie", "setcookie",
}
# String fields run through the secret-pattern redactor.
_FREE_TEXT_FIELDS = {
    "msg", "task", "grid", "screen_excerpt", "summary", "stderr",
    "traceback", "error", "err", "exception", "answer",
}


def _mask_if_opaque(m) -> str:
    """_LONGTOK_SEP callback: mask the run only if it spans digit+upper+lower
    (an opaque token), otherwise leave it (a benign identifier / UUID)."""
    tok = m.group(0)
    if (any(c.isdigit() for c in tok)
            and any(c.isupper() for c in tok)
            and any(c.islower() for c in tok)):
        return "***"
    return tok


def redact(text: str) -> str:
    # Specific masks first, then generic: a DOTTED prefixed secret (JWT
    # 'eyJ<hdr>.<payload>.<sig>', dotted sk-/ghp_ keys) must be masked WHOLE by
    # _PREFIXED/_BEARER (whose charclass includes '.') before the generic opaque
    # masks (_LONGTOK_SEP's charclass excludes '.') can split it and leak the tail.
    s = _KV.sub(lambda m: m.group(1) + "***", text)
    s = _PREFIXED.sub("***", s)
    s = _BEARER.sub("Bearer ***", s)
    s = _LONGTOK_SEP.sub(_mask_if_opaque, s)   # separator-bearing opaque (whole token)
    s = _LONGTOK.sub("***", s)                  # contiguous opaque (no separators)
    return s


def _norm_key(k: str) -> str:
    return k.replace("_", "").replace("-", "").lower()


def _redact_fields(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if _norm_key(k) in _SECRET_FIELDS:
            out[k] = "***"
        elif k in _FREE_TEXT_FIELDS and isinstance(v, str):
            out[k] = redact(v)
        else:
            out[k] = v               # numbers/bools/None and structural strings untouched
    return out


class Logger:
    """Generic leveled JSONL logger. `category="audit"` bypasses the level threshold."""

    def __init__(self, level="info", stream=None, audit_stream=None):
        self._threshold = _LEVELS.get(str(level).lower(), 20)
        self._stream = stream if stream is not None else sys.stderr
        self._audit = audit_stream if audit_stream is not None else self._stream

    def emit(self, level, component, event, session_id=None, *, category=None, **fields):
        if category != "audit" and _LEVELS.get(level, 20) < self._threshold:
            return
        rec = {"ts": _now(), "level": level, "component": component,
               "event": event, "session_id": session_id}
        if category is not None:
            rec["category"] = category
        rec.update(_redact_fields(fields))
        target = self._audit if category == "audit" else self._stream
        target.write(json.dumps(rec, ensure_ascii=False) + "\n")
        target.flush()

    def debug(self, component, event, session_id=None, **fields):
        self.emit("debug", component, event, session_id, **fields)

    def info(self, component, event, session_id=None, **fields):
        self.emit("info", component, event, session_id, **fields)

    def warning(self, component, event, session_id=None, **fields):
        self.emit("warning", component, event, session_id, **fields)

    def error(self, component, event, session_id=None, exc_info=False, **fields):
        if exc_info:
            fields["traceback"] = _traceback.format_exc()
        self.emit("error", component, event, session_id, **fields)

    # ---- audit: orthogonal to severity, always written ----
    def audit_decision(self, session_id, executor, kind, event_id, grid):
        self.emit("info", "decision", kind, session_id, category="audit",
                  executor=executor, event_id=event_id, grid=grid)

    def audit_task(self, session_id, executor, task):
        self.emit("info", "task_delivered", "task_delivered", session_id,
                  category="audit", executor=executor, task=task)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")
