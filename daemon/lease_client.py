"""Generation-to-router lease API client (§3.3b + §3.3c).

The daemon connects to the router's AF_UNIX socket (``NELIX_ROUTER_SOCK``) using the same
peercred auth the router uses for all same-host connections. The router's ``peer_is_self()``
check allows any same-uid caller, so no additional auth is needed.

S3b:
- ``reconciliation_id`` is tracked from router responses; every request carries it.
- ``transition_revision`` is a per-epoch monotonic counter bumped on each request.
- ``register_snapshot()`` implements the 6-step handshake for router restart recovery.
- A retry-until-ack outbox holds release token_ids that failed to reach the router;
  they are retried until acknowledged.
- On stale-id/REBUILDING error, the client detects the new reconciliation id from the
  error envelope and adopts it.
"""
import json
import threading

from nelix_contracts.errors import NelixError


class LeaseClient:
    """HTTP client for the router's lease acquire/release/register_snapshot endpoints.

    ``acquire`` returns a dict mapping kind -> ``{"token_id": str, "fresh": bool}``.
    ``fresh`` is True when this call created the lease; False when the key was already held
    (the caller must NOT release on rollback).

    Raises ``RouterUnavailable`` when the router cannot be reached — callers map this to
    ``admission_unavailable`` for retryable error propagation.
    """

    class RouterUnavailable(Exception):
        """The router's lease service could not be reached."""

    def __init__(self, router_sock_path: str, timeout=5,
                 generation_id=None, generation_epoch=None):
        self._path = router_sock_path
        self._timeout = timeout
        self._reconciliation_id = None
        self._transition_revision = 0
        self._gen_id = generation_id or ""
        self._gen_epoch = generation_epoch or ""
        self._lock = threading.Lock()
        self._release_outbox = {}
        # FIX 2: rollover-once guard — the last reconciliation_id we handshook under
        self._handshook_rid = None

    @property
    def reconciliation_id(self):
        with self._lock:
            return self._reconciliation_id

    @reconciliation_id.setter
    def reconciliation_id(self, value):
        with self._lock:
            self._reconciliation_id = value

    @property
    def transition_revision(self):
        with self._lock:
            return self._transition_revision

    def _next_revision(self):
        with self._lock:
            self._transition_revision += 1
            return self._transition_revision

    def _update_from_response(self, data):
        """Extract reconciliation_id from a router response (success or error)."""
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
        """True if we haven't yet handshook under the current reconciliation id."""
        with self._lock:
            return (self._reconciliation_id is not None
                    and self._handshook_rid != self._reconciliation_id)

    # ── outbox (retry-until-ack) ───────────────────────────────────────────

    def outbox_pending(self):
        with self._lock:
            return dict(self._release_outbox)

    def outbox_size(self):
        with self._lock:
            return len(self._release_outbox)

    def _outbox_add(self, token_id, revision, rid):
        with self._lock:
            self._release_outbox[token_id] = {"revision": revision, "rid": rid}

    def _outbox_remove(self, token_id):
        with self._lock:
            return self._release_outbox.pop(token_id, None) is not None

    def outbox_ack(self, token_id):
        """Acknowledge a release — remove from outbox. Returns True if existed."""
        return self._outbox_remove(token_id)

    def outbox_drain_upto(self, revision):
        """Delete all outbox entries with revision <= revision. Returns count removed."""
        removed = 0
        with self._lock:
            to_del = [tid for tid, info in self._release_outbox.items()
                      if info["revision"] <= revision]
            for tid in to_del:
                del self._release_outbox[tid]
                removed += 1
        return removed

    def retry_outbox(self):
        """Retry pending outbox releases. Returns list of still-pending token_ids."""
        pending = []
        with self._lock:
            snapshot = dict(self._release_outbox)
        for tid, info in snapshot.items():
            try:
                self._do_release(tid, override_rid=info["rid"],
                                 override_revision=info["revision"])
                # FIX 7: released:true OR released:false = acknowledged
                self._outbox_remove(tid)
            except (self.RouterUnavailable, NelixError):
                pending.append(tid)
        return pending

    # ── internal helpers ───────────────────────────────────────────────────

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

    def _do_release(self, token_id, override_rid=None, override_revision=None):
        """Execute a release call. Used by public release() and retry_outbox()."""
        body = {"token_id": token_id}
        if override_rid:
            body["reconciliation_id"] = override_rid
        if override_revision is not None:
            body["transition_revision"] = override_revision
        status, data = self._call("/lease/release", body)
        if status == 200:
            self._update_from_response(data)
            return data
        err = data.get("error", {})
        self._update_from_response(err)
        code = err.get("code", "internal_error")
        msg = err.get("message", "lease release failed")
        raise NelixError(code, msg)

    def _extract_error_rid(self, data):
        """Extract reconciliation_id from an error response envelope."""
        rid = data.get("reconciliation_id")
        if rid is not None:
            with self._lock:
                self._reconciliation_id = rid
        err_data = data.get("error", {})
        rid2 = err_data.get("reconciliation_id")
        if rid2 is not None:
            with self._lock:
                self._reconciliation_id = rid2

    # ── acquire ────────────────────────────────────────────────────────────

    def acquire(self, generation_id, generation_epoch, session_id, activation_id,
                kinds):
        """Acquire leases from the router.

        ``kinds`` is an iterable of ``"active"`` and/or ``"live"``.

        Returns a dict mapping kind -> ``{"token_id": str, "fresh": bool}``.

        Raises:
            LeaseClient.RouterUnavailable: router cannot be reached.
            NelixError: router returned an error envelope (STALE_RECONCILIATION_ID,
                        REBUILDING, CONCURRENCY_LIMIT, …).
        """
        revision = self._next_revision()
        with self._lock:
            current_rid = self._reconciliation_id
        body = {
            "generation_id": generation_id,
            "generation_epoch": generation_epoch,
            "session_id": session_id,
            "activation_id": str(activation_id),
            "kinds": list(kinds),
            "transition_revision": revision,
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
        # FIX 2: extract reconciliation_id from error envelope
        self._extract_error_rid(data)
        err = data.get("error", {})
        code = err.get("code", "internal_error")
        msg = err.get("message", "lease acquire failed")
        raise NelixError(code, msg)

    # ── release (with retry-until-ack outbox) ──────────────────────────────

    def release(self, token_id):
        """Release a single lease token.

        On failure (router unreachable or rebuilding), the token is placed in a
        retry-until-ack outbox so it is retried until the router acknowledges it.
        Returns the release result or raises on non-retryable error.
        """
        revision = self._next_revision()
        with self._lock:
            current_rid = self._reconciliation_id
        body = {
            "token_id": token_id,
            "transition_revision": revision,
            "reconciliation_id": current_rid or "",
        }
        try:
            status, data = self._call("/lease/release", body)
        except (self.RouterUnavailable, OSError):
            self._outbox_add(token_id, revision, current_rid)
            return False
        if status == 200:
            self._update_from_response(data)
            released = data.get("released", False)
            # FIX 7: ack'd regardless of released:true/false
            self._outbox_remove(token_id)
            return released
        # Non-200: check if retryable.
        self._extract_error_rid(data)
        err_data = data.get("error", {})
        code = err_data.get("code", "internal_error")
        msg = err_data.get("message", "lease release failed")
        if err_data.get("retryable", False):
            self._outbox_add(token_id, revision, current_rid)
            return False
        raise NelixError(code, msg)

    # ── register_snapshot (6-step handshake) ──────────────────────────────

    def register_snapshot(self, generation_id, generation_epoch,
                          active_tokens, live_tokens, cutoff_revision):
        """Register a generation snapshot with the router (6-step handshake).

        ``active_tokens`` and ``live_tokens`` are lists of token dicts:
        ``{"token_id": str, "key": (gen_id, gen_epoch, session_id, activation_id)}``.

        Returns the router's response dict, which includes
        ``{"acknowledged_revision": cutoff_revision}``.

        Raises:
            LeaseClient.RouterUnavailable: router cannot be reached.
            NelixError: router returned an error envelope.
        """
        with self._lock:
            current_rid = self._reconciliation_id
        body = {
            "generation_id": generation_id,
            "generation_epoch": generation_epoch,
            "reconciliation_id": current_rid or "",
            "cutoff_revision": cutoff_revision,
            "active_tokens": active_tokens,
            "live_tokens": live_tokens,
        }
        status, data = self._call("/lease/register_snapshot", body)
        if status == 200:
            self._update_from_response(data)
            return data
        self._extract_error_rid(data)
        err = data.get("error", {})
        code = err.get("code", "internal_error")
        msg = err.get("message", "register_snapshot failed")
        raise NelixError(code, msg)
