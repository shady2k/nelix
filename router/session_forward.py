"""nelix-3rm slice 3c.2 Part A/B: SESSION-KEYED forwarding + nelix-80e S2b route matrix.

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

S2b ROUTE MATRIX (nelix-80e): every session-keyed request resolves session_id -> generation_id via
the StartLedger's `starts` row (threaded into __init__) and dispatches by the owning generation's
epoch process_state:

  * live/serving -> forward to the owning generation's handle (byte-identical to the N=1 path).
  * dead epoch / retired generation -> status+dialog resolve from archive/disk; screen/respond/stop/
    hook/message -> unsupported_by_generation; restart -> TODO(S4).

Forward-failure mapping: NONE of these routes are reserve-tracked like /start (no ledger row to
protect), so a transport failure of EITHER phase (connect or response) collapses to ONE retryable
GENERATION_UNAVAILABLE — never a bare 500. A SUCCESSFUL forward's (status, body) is returned
UNCHANGED: that response IS the generation's answer (including its own ownership verdict), and this
module must never reinterpret it.
"""
import urllib.parse

from nelix_contracts.errors import (
    INVALID_REQUEST, UNSUPPORTED_BY_GENERATION, NelixError,
)
from nelix_contracts.ids import InvalidId, validate_owner_id, validate_session_id

from router.forwarding import relay

try:
    from rpc_client import RpcClient, raw_forward
except ImportError:
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


def _resolve_and_forward(forward, owner_id, sid, params_fn, method, path_tpl,
                         body_fn=None):
    """Resolve session -> generation state and dispatch via route matrix.

    If the session is NOT in the ledger (or ledger is None), falls back to
    forwarding to registry.active() — the pre-S2b behaviour.
    """
    if forward._ledger is not None:
        try:
            gen_id, gen_epoch = forward._ledger.get_session_generation(sid)
            proc_state, lc_state, cap_snap, handle = \
                forward._registry.resolve_generation_state(gen_id, gen_epoch)
        except NelixError:
            proc_state = None
            handle = None
        if proc_state is not None and handle is not None:
            path, body = params_fn(sid, owner_id)
            return forward._forward_handle(handle, owner_id, method, path, body)
        if proc_state is not None and proc_state != "serving":
            return forward._route_archived(sid, owner_id, method, path_tpl, body_fn)
    gen = forward._registry.active()
    path, body = params_fn(sid, owner_id)
    return forward._forward_handle(gen, owner_id, method, path, body)


class SessionForward:
    def __init__(self, registry, ledger=None, store=None):
        self._registry = registry
        self._ledger = ledger
        self._store = store

    def _forward_handle(self, gen, owner_id, method, path, body=None):
        client = RpcClient(gen.transport, owner_id)
        return relay(lambda: client.forward_raw(method, path, body))

    def _unsupported(self):
        raise NelixError(UNSUPPORTED_BY_GENERATION,
                         "this session's generation is no longer serving")

    def _route_archived(self, sid, owner_id, method, path_tpl, body_fn):
        """Route for dead-epoch / retired generations — read archive or raise unsupported."""
        if path_tpl == "status":
            if self._store is not None:
                try:
                    terminal = self._store.get_terminal(sid, owner_id=owner_id)
                    return 200, {
                        "session_id": sid,
                        "terminal_kind": terminal.terminal_kind,
                        "screen_excerpt": terminal.summary,
                        "control_state": "terminal",
                        "pending": False,
                        "terminal": True,
                    }
                except NelixError:
                    pass
        elif path_tpl == "dialog":
            try:
                from daemon.dialog import DialogReader
                import paths
                sdir = paths.sessions_root() / sid
                reader = DialogReader(sdir)
                if reader.available:
                    params = body_fn(sid, owner_id) if body_fn else {}
                    page = reader.page(offset=params.get("offset", 0),
                                       limit=params.get("limit"))
                    page["at_end"] = page["next_offset"] >= page["total_len"]
                    return 200, page
            except Exception:
                pass
        elif path_tpl == "restart":
            pass  # TODO(S4): spawn on active generation
        self._unsupported()

    # -------------------------------------------------------------- owner-scoped routes

    def status(self, owner_id, session_id, include_progress=None):
        owner_id = _owner(owner_id)
        sid = _session(session_id)

        def params(sid, owner_id):
            p = {"session_id": sid, "owner_id": owner_id}
            if include_progress is not None:
                p["include_progress"] = include_progress
            return "/status?" + urllib.parse.urlencode(p), None

        return _resolve_and_forward(self, owner_id, sid, params,
                                    "GET", "status", None)

    def dialog(self, owner_id, session_id, offset=None, limit=None):
        owner_id = _owner(owner_id)
        sid = _session(session_id)

        def params(sid, owner_id):
            p = {"session_id": sid, "owner_id": owner_id}
            if offset is not None:
                p["offset"] = offset
            if limit is not None:
                p["limit"] = limit
            return "/dialog?" + urllib.parse.urlencode(p), None

        def body_fn(sid, owner_id):
            p = {}
            if offset is not None:
                p["offset"] = offset
            if limit is not None:
                p["limit"] = limit
            return p

        return _resolve_and_forward(self, owner_id, sid, params,
                                    "GET", "dialog", body_fn)

    def screen(self, owner_id, session_id, raw=None, force=None):
        owner_id = _owner(owner_id)
        sid = _session(session_id)

        def params(sid, owner_id):
            p = {"session_id": sid, "owner_id": owner_id}
            if raw is not None:
                p["raw"] = raw
            if force is not None:
                p["force"] = force
            return "/screen?" + urllib.parse.urlencode(p), None

        return _resolve_and_forward(self, owner_id, sid, params,
                                    "GET", "screen", None)

    def respond(self, owner_id, session_id, answer, decision_id=None):
        owner_id = _owner(owner_id)
        sid = _session(session_id)

        def params(sid, owner_id):
            body = {"session_id": sid, "answer": answer, "owner_id": owner_id}
            if decision_id is not None:
                body["decision_id"] = decision_id
            return "/respond", body

        return _resolve_and_forward(self, owner_id, sid, params,
                                    "POST", "respond", None)

    def stop(self, owner_id, session_id):
        owner_id = _owner(owner_id)
        sid = _session(session_id)

        def params(sid, owner_id):
            return "/stop", {"session_id": sid, "owner_id": owner_id}

        return _resolve_and_forward(self, owner_id, sid, params,
                                    "POST", "stop", None)

    def restart(self, owner_id, session_id, *, new_session_id=None, force=None):
        owner_id = _owner(owner_id)
        sid = _session(session_id)

        def params(sid, owner_id):
            body = {"session_id": sid, "owner_id": owner_id}
            if new_session_id is not None:
                body["new_session_id"] = new_session_id
            if force is not None:
                body["force"] = force
            return "/restart", body

        return _resolve_and_forward(self, owner_id, sid, params,
                                    "POST", "restart", None)

    # -------------------------------------------------------------- owner-EXEMPT executor plane

    def forward_secret(self, method, path, headers, body):
        _validate_secret_plane_sid(path)
        sid = _session_id_from_path(path)
        if self._ledger is not None:
            try:
                gen_id, gen_epoch = self._ledger.get_session_generation(sid)
                proc_state, lc_state, cap_snap, handle = \
                    self._registry.resolve_generation_state(gen_id, gen_epoch)
            except NelixError:
                proc_state = None
                handle = None
            if proc_state is not None and handle is not None:
                return relay(lambda: raw_forward(handle.transport, method, path,
                                                 headers=headers, body=body))
            if proc_state is not None and proc_state != "serving":
                self._unsupported()
        gen = self._registry.active()
        return relay(lambda: raw_forward(gen.transport, method, path,
                                         headers=headers, body=body))


_SECRET_PLANE_PREFIXES = ("/hook/", "/message/")


def _validate_secret_plane_sid(path):
    for prefix in _SECRET_PLANE_PREFIXES:
        if path.startswith(prefix):
            _session(path[len(prefix):])
            return


def _session_id_from_path(path):
    for prefix in _SECRET_PLANE_PREFIXES:
        if path.startswith(prefix):
            return path[len(prefix):]
    return None
