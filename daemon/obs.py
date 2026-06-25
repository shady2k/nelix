import json
import re
import sys
import time
import traceback as _traceback

# Mask: key=value where value looks secret-ish, and standalone long tokens.
_KV = re.compile(r"((?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*)(\S+)", re.I)
_LONGTOK = re.compile(r"\b[A-Za-z0-9_\-]{16,}\b")
# Mask: known secret prefixes (Bearer, sk-, ghp_, gho_, ghs_, xox, AKIA, eyJ) regardless of length
_PREFIXED = re.compile(r"\b(?:sk-|ghp_|gho_|ghs_|xox[a-z]-|AKIA|eyJ)[A-Za-z0-9._\-]+")
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
    "traceback", "error", "err", "exception",
}


def redact(text: str) -> str:
    s = _KV.sub(lambda m: m.group(1) + "***", text)
    s = _LONGTOK.sub("***", s)
    s = _PREFIXED.sub("***", s)
    s = _BEARER.sub("Bearer ***", s)
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
