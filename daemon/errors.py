"""Shared daemon exceptions, and the stable machine-error envelope (spec §10). Zero imports on
purpose — safe to import from any layer (pty_session, session, rpc_server) without triggering
package __init__ import cycles."""


def error_envelope(code: str, message: str, *, retryable: bool) -> dict:
    """`{"error": {"code", "message", "retryable"}}` (spec §10: "Stable machine error codes
    `{error:{code,message,retryable}}`.") — a caller can branch on `code` without parsing prose.

    `retryable` is keyword-only and never defaulted: it is a fact about the CODE (does retrying
    the same call have any chance of succeeding), not a guess the helper should make on a
    caller's behalf.

    Additive, not a replacement: existing routes' bare `{"error": "..."}` responses are untouched
    (rewriting them is out of scope — see nelix-9a4.6's brief). This is used only for the NEW
    error cases this task introduces (`unknown_session` on /capabilities, `session_id_in_use` /
    `invalid_session_id` on /start's new `session_id` param), so no existing caller's parsing of
    an existing route breaks.
    """
    return {"error": {"code": code, "message": message, "retryable": retryable}}


class PtyWriteTimeout(Exception):
    """Raised by a handle's write() when `data` could not be fully written before the
    deadline — e.g. the executor stopped draining its stdin and the PTY input buffer
    filled. Prevents a blocking write from wedging the monitor thread forever."""

    def __init__(self, written, total):
        super().__init__(f"wrote {written}/{total} bytes before write timeout")
        self.written = written
        self.total = total
