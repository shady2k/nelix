"""The `cli_api` v1 response envelope (spec 2026-07-21-nelix-claude-code-plugin-design §3.1).

One rule, enforced in one place: every verb prints EXACTLY ONE JSON object on stdout — on success
and on failure — and writes human diagnostics to stderr only. A caller can therefore parse stdout
unconditionally, and a shell can branch on the exit class without parsing anything.

`1` is deliberately NOT used as an exit class: it stays reserved for an unclassified failure (an
uncaught traceback), so a crash can never be mistaken for a classified outcome.
"""
import http.client
import json
import sys

CLI_API = 1

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_INCOMPATIBLE = 4
EXIT_REJECTED = 5

# What a router call can genuinely fail with: no socket / refused / dropped connection (OSError
# family), a malformed HTTP reply, or a non-JSON body (ValueError from json.loads). Narrow on
# purpose — a bug in our own code must surface as a real traceback, not as "router unavailable".
ROUTER_ERRORS = (OSError, http.client.HTTPException, ValueError)


def _print(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def emit_ok(payload: dict) -> int:
    """Print one success object: the payload under the fixed envelope keys. Envelope keys win over
    payload keys, so a router body that happens to carry `ok` cannot forge the envelope."""
    _print({**payload, "cli_api": CLI_API, "ok": True})
    return EXIT_OK


def emit_error(code: str, message: str, *, exit_class: int, details: dict | None = None) -> int:
    """Print one failure object AND the human message on stderr; return the exit class."""
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    _print({"cli_api": CLI_API, "ok": False, "error": error})
    print(message, file=sys.stderr)
    return exit_class
