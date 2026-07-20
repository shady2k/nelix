"""Generation-to-router lease API client (§3.3b + §3.3c).

The daemon connects to the router's AF_UNIX socket (``NELIX_ROUTER_SOCK``) using the same
peercred auth the router uses for all same-host connections.

S3b:
- ``reconciliation_id`` is tracked from router responses; every request carries it.
- ``register_snapshot()`` implements the 6-step handshake for router restart recovery.
- A retry-until-ack outbox holds release token_ids that failed to reach the router;
  they are retried until acknowledged. Only removed on definitive ack
  (released:true/false from a reconciled epoch).
"""
import json
import threading

from nelix_contracts.errors import NelixError


class LeaseClient:
    """HTTP client for the router's lease acquire/release/register_snapshot endpoints."""

    class RouterUnavailable(Exception):
        """The router's lease service could not be reached."""

    def __init__(self, router_sock_path: str, timeout=5,
                 generation_id=None, generation_epoch=None):
        self._path = router_sock_path
        self._timeout = timeout
        self._reconciliation_id = None
        self._gen_id = generation_id or ""
        self._gen_epoch = generation_epoch or ""
        self._lock = threading.Lock()
        self._release_outbox = {}
        self._handshook_rid = None

    @property
    def reconciliation_id(self):
        with self._lock:
            return self._reconciliation_id

    @reconciliation_id.setter
    def reconciliation_id(self, value):
        with self._lock:
            self._reconciliation_id = value

    def _update_from_response(self, data):
        rid = data.get("reconciliation_id")
        if rid is not None:
            with self._lock:
                self._reconciliation_id = rid

    @property
    def handshook_rid(self):
        with self._lock:
            return self._handshook_rid

    def mark_handshook(self, rid):
        with self._lock:
            self._handshook_rid = rid

    def needs_handshake(self):
        with self._lock:
            return (self._reconciliation_id is not None
                    and self._handshook_rid != self._reconciliation_id)

    # ── outbox ─────────────────────────────────────────────────────────────

    def outbox_pending(self):
        with self._lock:
            return dict(self._release_outbox)

    def outbox_size(self):
        with self._lock:
            return len(self._release_outbox)

    def _outbox_add(self, token_id):
        with self._lock:
            self._release_outbox[token_id] = True

    def _outbox_remove(self, token_id):
        with self._lock:
            return self._release_outbox.pop(token_id, None) is not None

    def outbox_ack(self, token_id):
        return self._outbox_remove(token_id)

    def retry_outbox(self):
        """Retry pending outbox releases with the CURRENT reconciliation id.

        Removes entry on definitive ack (released:true OR released:false from a
        reconciled epoch). Returns list of token_ids still pending.
        """
        pending = []
        with self._lock:
            current_rid = self._reconciliation_id
            snapshot = list(self._release_outbox.keys())
        for tid in snapshot:
            try:
                self._do_release(tid, override_rid=current_rid)
                self._outbox_remove(tid)
            except (self.RouterUnavailable, NelixError):
                pending.append(tid)
        return pending

    # ── helpers ────────────────────────────────────────────────────────────

    def _call(self, endpoint, body):
        from rpc_client import UnixHTTPConnection
        import http.client
        try:
            conn = UnixHTTPConnection(self._path, timeout=self._timeout)
            try:
                conn.request(
                    "POST", endpoint,
                    body=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                data = json.loads(resp.read() or b"{}")
                return resp.status, data
            finally:
                conn.close()
        except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
            raise self.RouterUnavailable(str(e)) from e
        except http.client.RemoteDisconnected as e:
            raise self.RouterUnavailable(str(e)) from e

    def _do_release(self, token_id, override_rid=None):
        """Execute a release call (used by release() and retry_outbox())."""
        body = {
            "token_id": token_id,
            "generation_id": self._gen_id,
            "generation_epoch": self._gen_epoch,
        }
        if override_rid:
            body["reconciliation_id"] = override_rid
        status, data = self._call("/lease/release", body)
        if status == 200:
            self._update_from_response(data)
            return data
        # FIX 6: read reconciliation_id from TOP LEVEL, not data[error].
        self._update_from_response(data)
        err = data.get("error", {})
        code = err.get("code", "internal_error")
        msg = err.get("message", "lease release failed")
        raise NelixError(code, msg)

    # ── acquire ────────────────────────────────────────────────────────────

    def acquire(self, generation_id, generation_epoch, session_id, activation_id,
                kinds):
        with self._lock:
            current_rid = self._reconciliation_id
        body = {
            "generation_id": generation_id,
            "generation_epoch": generation_epoch,
            "session_id": session_id,
            "activation_id": str(activation_id),
            "kinds": list(kinds),
            "reconciliation_id": current_rid or "",
        }
        status, data = self._call("/lease/acquire", body)
        if status == 200:
            self._update_from_response(data)
            tokens = data.get("tokens", {})
            if not tokens:
                raise self.RouterUnavailable(
                    "lease acquire returned 200 without tokens")
            return tokens
        self._update_from_response(data)
        err = data.get("error", {})
        code = err.get("code", "internal_error")
        msg = err.get("message", "lease acquire failed")
        raise NelixError(code, msg)

    # ── release ────────────────────────────────────────────────────────────

    def release(self, token_id):
        """Release a single token.

        On retryable failure (REBUILDING, router unavailable), the token stays
        in the outbox for retry. On definitive ack (released:true/false from a
        reconciled epoch), removed from outbox.
        """
        with self._lock:
            current_rid = self._reconciliation_id
        body = {
            "token_id": token_id,
            "generation_id": self._gen_id,
            "generation_epoch": self._gen_epoch,
            "reconciliation_id": current_rid or "",
        }
        try:
            status, data = self._call("/lease/release", body)
        except (self.RouterUnavailable, OSError):
            self._outbox_add(token_id)
            return False
        if status == 200:
            self._update_from_response(data)
            released = data.get("released", False)
            self._outbox_remove(token_id)
            return released
        # Non-200: retryable or not.
        self._update_from_response(data)
        err_data = data.get("error", {})
        code = err_data.get("code", "internal_error")
        msg = err_data.get("message", "lease release failed")
        if err_data.get("retryable", False):
            self._outbox_add(token_id)
            return False
        raise NelixError(code, msg)

    # ── register_snapshot ──────────────────────────────────────────────────

    def register_snapshot(self, generation_id, generation_epoch,
                          active_tokens, live_tokens, reconciliation_id):
        """Register a generation snapshot with the router.

        ``reconciliation_id`` is the EXACT id to use for this registration
        (the captured target_rid, not the current client id which may have
        advanced).
        """
        body = {
            "generation_id": generation_id,
            "generation_epoch": generation_epoch,
            "reconciliation_id": reconciliation_id or "",
            "active_tokens": active_tokens,
            "live_tokens": live_tokens,
        }
        status, data = self._call("/lease/register_snapshot", body)
        if status == 200:
            self._update_from_response(data)
            return data
        self._update_from_response(data)
        err = data.get("error", {})
        code = err.get("code", "internal_error")
        msg = err.get("message", "register_snapshot failed")
        raise NelixError(code, msg)
