"""nelix-3rm slice 3c.2 Part A/B: SESSION-KEYED forwarding.

Two disjoint auth planes share this module because both are session-keyed by routing.classify
(design §1) even though they authenticate completely differently:

  * The OWNER routes (respond/stop/restart/screen/dialog/session-scoped status): AUTH PASSTHROUGH
    is the whole point (spec §7). The router validates owner_id's SHAPE (same check /start already
    applies) so a malformed owner_id is a clean 400 before ever reaching the wire, then forwards it
    UNCHANGED to the generation, which alone decides ownership (`owner.owns_session`, daemon-side).
    This module must NEVER re-implement that check (a router-side allow/deny would be a second,
    divergent gate) and must NEVER weaken it — relaying the generation's response FAITHFULLY,
    whatever shape it takes (a 200 with an error body, a 404, ...), is what keeps the ONE real gate
    intact all the way through the router. That is the spec ownership test: harness X must not
    reach harness Y's session on ANY of these routes, and it is enforced entirely by the daemon,
    reached intact.

  * The executor-facing plane (/hook/<sid>, /message/<sid>): owner-EXEMPT by design (a worker is
    not a caller — it authenticates with a per-session secret, a STRONGER check than an owner id).
    No owner_id is validated, constructed, or forwarded here at all; the secret header and the raw
    body are passed through byte-for-byte. The <sid> path component IS shape-validated first (review
    fix pass), for consistency with the owner routes' fail-fast pattern — not a security boundary
    (the daemon re-validates independently, and prefix routing cannot traverse out of a session id),
    just the same clean-400-before-the-wire discipline applied here too.

ROUTING (structural seam for Plan 4): every method below resolves to `registry.active()` — the one
generation the registry tracks today (N=1). When the registry holds N>1 generations, a session-keyed
request must instead resolve session_id -> generation_id via the StartLedger's `starts` row (the
row /start's forward already commits the session to, see router/start.py) and route to THAT
generation's handle, not the active pointer. Leaving every call here as `registry.active()` — rather
than threading a ledger lookup nothing can act on yet — keeps that a one-line change per method
instead of a reshape when Plan 4 lands.

nelix-3rm 3c.4 (router restart/reconcile) verified the N=1 case of this exact seam end-to-end
against a real daemon: the StartLedger is durable (SQLite under NELIX_HOME), so a FRESH router
process's FRESH StartLedger instance already sees every pre-restart session's `starts` row with no
reconcile step of its own — a router restart never loses that data, it just stops caching it in
memory. With one tracked slot, "resolve session_id -> generation" collapses to "the one discovered
generation" (`registry.active()`), so 3c.4 needed no new mechanism here. Plan 4's job when N>1 is
exactly the lookup this paragraph already describes — the ledger already has what it needs; only the
lookup (and the loop this module's single call site becomes) is unbuilt.

Forward-failure mapping: NONE of these routes are reserve-tracked like /start (no ledger row to
protect), so a transport failure of EITHER phase (connect or response) collapses to ONE retryable
GENERATION_UNAVAILABLE — never a bare 500. A SUCCESSFUL forward's (status, body) is returned
UNCHANGED: that response IS the generation's answer (including its own ownership verdict), and this
module must never reinterpret it.
"""
import urllib.parse

from nelix_contracts.errors import INVALID_REQUEST, NelixError
from nelix_contracts.ids import InvalidId, validate_owner_id, validate_session_id

from router.forwarding import relay

try:
    from rpc_client import RpcClient, raw_forward
except ImportError:                                          # package mode
    from .rpc_client import RpcClient, raw_forward


def _owner(value):
    try:
        return validate_owner_id(value)
    except InvalidId as e:
        raise NelixError(INVALID_REQUEST, str(e)) from None


def _session(value):
    if value is None:
        raise NelixError(INVALID_REQUEST, "session_id is required")
    try:
        return validate_session_id(value)
    except InvalidId as e:
        raise NelixError(INVALID_REQUEST, str(e)) from None


class SessionForward:
    def __init__(self, registry):
        self._registry = registry

    def _forward(self, owner_id, method, path, body=None):
        gen = self._registry.active()          # Plan-4 seam: session_id -> generation_id (see
                                                 # module docstring); N=1 collapses it to active()
        client = RpcClient(gen.transport, owner_id)
        return relay(lambda: client.forward_raw(method, path, body))

    # -------------------------------------------------------------- owner-scoped routes

    def status(self, owner_id, session_id, include_progress=None):
        owner_id = _owner(owner_id)
        sid = _session(session_id)
        params = {"session_id": sid, "owner_id": owner_id}
        if include_progress is not None:
            params["include_progress"] = include_progress
        return self._forward(owner_id, "GET", "/status?" + urllib.parse.urlencode(params))

    def dialog(self, owner_id, session_id, offset=None, limit=None):
        owner_id = _owner(owner_id)
        sid = _session(session_id)
        params = {"session_id": sid, "owner_id": owner_id}
        if offset is not None:
            params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        return self._forward(owner_id, "GET", "/dialog?" + urllib.parse.urlencode(params))

    def screen(self, owner_id, session_id, raw=None, force=None):
        owner_id = _owner(owner_id)
        sid = _session(session_id)
        params = {"session_id": sid, "owner_id": owner_id}
        if raw is not None:
            params["raw"] = raw
        if force is not None:
            params["force"] = force
        return self._forward(owner_id, "GET", "/screen?" + urllib.parse.urlencode(params))

    def respond(self, owner_id, session_id, answer, decision_id=None):
        owner_id = _owner(owner_id)
        sid = _session(session_id)
        body = {"session_id": sid, "answer": answer, "owner_id": owner_id}
        if decision_id is not None:
            body["decision_id"] = decision_id
        return self._forward(owner_id, "POST", "/respond", body)

    def stop(self, owner_id, session_id):
        owner_id = _owner(owner_id)
        sid = _session(session_id)
        return self._forward(owner_id, "POST", "/stop", {"session_id": sid, "owner_id": owner_id})

    def restart(self, owner_id, session_id, *, new_session_id=None, force=None):
        owner_id = _owner(owner_id)
        sid = _session(session_id)
        body = {"session_id": sid, "owner_id": owner_id}
        if new_session_id is not None:
            body["new_session_id"] = new_session_id
        if force is not None:
            body["force"] = force
        return self._forward(owner_id, "POST", "/restart", body)

    # -------------------------------------------------------------- owner-EXEMPT executor plane

    def forward_secret(self, method, path, headers, body):
        """POST /hook/<sid> or /message/<sid>: no owner_id anywhere — authenticated purely by the
        per-session secret HEADER the caller already supplied (in `headers`, passed through
        unchanged) and forwarded with the RAW `body` bytes, untouched. Same Plan-4 routing seam as
        the owner-scoped methods above (today: the active generation).

        The <sid> path component is shape-validated FIRST (the same validate_session_id the
        owner-scoped routes' `_session()` applies), for consistency with the router's fail-fast
        pattern — every owner-scoped route above rejects a malformed session_id before ever
        forwarding. This is not a new security boundary (the daemon independently re-validates, and
        prefix-based path routing cannot traverse out of a session id): it just stops a bad-shape
        sid from reaching the wire at all here too. The secret header and raw body are never
        touched by this check and are forwarded byte-for-byte unchanged on success."""
        _validate_secret_plane_sid(path)
        gen = self._registry.active()
        return relay(lambda: raw_forward(gen.transport, method, path, headers=headers, body=body))


_SECRET_PLANE_PREFIXES = ("/hook/", "/message/")


def _validate_secret_plane_sid(path):
    """Extract and shape-validate the <sid> in /hook/<sid> or /message/<sid> (the only two paths
    forward_secret is ever called with — server.py only dispatches here after matching one of these
    prefixes). Raises NelixError(INVALID_REQUEST) on a bad shape, same as `_session()` above."""
    for prefix in _SECRET_PLANE_PREFIXES:
        if path.startswith(prefix):
            _session(path[len(prefix):])
            return
