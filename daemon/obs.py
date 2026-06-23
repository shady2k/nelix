import json
import re
import sys
import time

# Mask: key=value where value looks secret-ish, and standalone long tokens.
_KV = re.compile(r"((?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*)(\S+)", re.I)
_LONGTOK = re.compile(r"\b[A-Za-z0-9_\-]{16,}\b")
# Mask: known secret prefixes (Bearer, sk-, ghp_, gho_, ghs_, xox, AKIA, eyJ) regardless of length
_PREFIXED = re.compile(r"\b(?:sk-|ghp_|gho_|ghs_|xox[a-z]-|AKIA|eyJ)[A-Za-z0-9._\-]+")
# Mask: Bearer <token> form
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")


def redact(text: str) -> str:
    s = _KV.sub(lambda m: m.group(1) + "***", text)
    s = _LONGTOK.sub("***", s)
    s = _PREFIXED.sub("***", s)
    s = _BEARER.sub("Bearer ***", s)
    return s


class Logger:
    def __init__(self, stream=None, audit_stream=None):
        self._stream = stream if stream is not None else sys.stderr
        self._audit = audit_stream if audit_stream is not None else self._stream

    def _write(self, target, rec):
        rec["ts"] = _now()
        target.write(json.dumps(rec) + "\n")
        target.flush()

    def event(self, component, level, session_id=None, **fields):
        rec = {"level": level, "component": component, "session_id": session_id}
        rec.update(fields)
        self._write(self._stream, rec)

    def audit_decision(self, session_id, executor, kind, event_id, grid):
        self._write(self._audit, {
            "level": "audit", "component": "decision", "session_id": session_id,
            "executor": executor, "kind": kind, "event_id": event_id,
            "grid": redact(grid),
        })


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")
