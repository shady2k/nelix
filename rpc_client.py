import http.client
import json
import socket
import urllib.parse

try:
    from .daemon.transport import Transport     # package mode (hermes_plugins.nelix.rpc_client)
    from .daemon import owner
except ImportError:
    from daemon.transport import Transport      # top-level module mode (tests)
    from daemon import owner


class ForwardConnectError(Exception):
    """A forward that failed BEFORE the request was fully delivered — the connection could not be
    established, or the send did not complete (connection refused, host/network unreachable, a
    permission/path error on connect, a DNS failure, a connect timeout, or a broken send). No
    complete request reached the generation, so NO worker was created: the router may treat this as a
    DEFINITE failure and record it, rather than stranding the reservation (nelix-3rm 3c.1 finding #2).
    Classification is by PHASE (which step raised), not by exception type — so a PermissionError or a
    generic OSError on connect is definite, not lumped with the ambiguous post-send failures."""


class ForwardResponseError(Exception):
    """A forward that delivered the request but then failed while AWAITING or READING the response —
    a read timeout, the connection dropped after the request was sent, or a malformed/garbled reply.
    The generation may already have created the worker, so this is AMBIGUOUS: the router must NOT
    record a durable failure (that would strand an orphan worker behind a false `failed`); it leaves
    the reservation `starting` and returns a retryable error (nelix-3rm 3c.1 findings #2/#3)."""


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def _phase_split(connect, method, path, body, headers):
    """The phase-split forward mechanic shared by every forward in this module (RpcClient's owner-
    scoped forward_raw AND the owner-EXEMPT raw_forward below): connect+send is the DEFINITE phase
    (a failure here means no complete request was delivered, so whatever the request would have
    caused definitely did not happen — ForwardConnectError); awaiting/reading the reply is the
    AMBIGUOUS phase (the request was sent, so it may already have taken effect —
    ForwardResponseError). Returns (status, decoded_json_body) UNCHANGED on success — this is the
    ONE place that decides definite-vs-ambiguous; every caller (start's /start forward, the
    router's session-keyed forward, the router's hook/message passthrough) reuses it rather than
    re-deriving the classification."""
    conn = None
    try:
        try:
            conn = connect()
            conn.request(method, path, body=body, headers=headers)
        except Exception as e:
            raise ForwardConnectError(str(e)) from e
        try:
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        except Exception as e:
            raise ForwardResponseError(str(e)) from e
    finally:
        if conn is not None:
            conn.close()


def raw_forward(transport, method, path, *, headers=None, body=None, timeout=30):
    """A transport-level phase-split forward carrying NO owner_id: sends `method path` with the
    caller-supplied `headers` (plus the transport's own tcp token, exactly like RpcClient._prep)
    and a RAW `body` (bytes or None, sent verbatim — never re-encoded), returning (status,
    decoded_json_body) UNCHANGED.

    For the router's owner-EXEMPT executor plane (/hook/<sid>, /message/<sid>, spec §7): those
    routes authenticate by a per-session secret HEADER the caller already supplied, never by
    owner_id, so there is no owner to construct an RpcClient with — fabricating one would be
    exactly the re-implemented auth the passthrough must avoid. Phase-split via `_phase_split`
    (see its docstring): a connect/send failure raises ForwardConnectError, a response-phase
    failure raises ForwardResponseError."""
    hdrs = dict(headers or {})
    if transport.kind == "tcp":
        hdrs.setdefault("X-Nelix-Token", transport.token)

    def _connect():
        if transport.kind == "unix":
            return UnixHTTPConnection(transport.path, timeout=timeout)
        return http.client.HTTPConnection(transport.host, transport.port, timeout=timeout)

    return _phase_split(_connect, method, path, body, hdrs)


class RpcClient:
    """A client speaking for ONE owner (daemon/owner.py).

    `owner_id` is a constructor argument, not a per-call one, because it is a property of the
    CALLER, not of the request: a client is one harness, and one harness is one owner for its
    whole life. Threading it per-call would invite a caller to vary it, and the first thing a
    caller varies it to is another owner's id. Required and shape-checked here so a tool that
    forgets it fails at construction, not with a 400 on its first read.
    """

    def __init__(self, transport, owner_id):
        self._t = transport
        self._owner = owner.validate(owner_id)

    def _conn(self, timeout):
        if self._t.kind == "unix":
            return UnixHTTPConnection(self._t.path, timeout=timeout)
        return http.client.HTTPConnection(self._t.host, self._t.port, timeout=timeout)

    def _prep(self, body):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self._t.kind == "tcp":
            headers["X-Nelix-Token"] = self._t.token
        return data, headers

    def _call(self, method, path, body=None, timeout=30):
        data, headers = self._prep(body)
        conn = self._conn(timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def forward_raw(self, method, path, body, timeout=30):
        """Like _call, but with the connect+send and the response-read phases SEPARATED (via the
        shared `_phase_split`) so the caller can tell a DEFINITE pre-delivery failure
        (ForwardConnectError — the request never fully left, so no worker/effect happened) from an
        AMBIGUOUS post-send one (ForwardResponseError — the request was sent, so a worker/effect may
        already exist). Returns (status, decoded_json_body) UNCHANGED — for a caller (the router's
        session-keyed forward) that must relay the generation's exact response, not just infer
        success from the body the way /start's own forward does (see _forward_call below)."""
        data, headers = self._prep(body)
        return _phase_split(lambda: self._conn(timeout), method, path, data, headers)

    def _forward_call(self, method, path, body, timeout=30):
        """/start's own forward: identical phase split (via forward_raw), but returns ONLY the
        decoded body — /start's success is decided by the generation's OWN reply body
        (status=="started" + the echoed session_id), not by the HTTP status code, so the status is
        discarded here rather than threaded through every existing caller of this method."""
        _, reply = self.forward_raw(method, path, body, timeout=timeout)
        return reply

    def start(self, executor, task, cwd, model=None, session_id=None, timeout=30):
        # model (nelix-9k0) and session_id (nelix-3rm) are only ADDED when provided, so a default
        # call is byte-for-byte the same request as before either option existed (mirrors status's
        # include_progress). session_id is the ROUTER-assigned id: the router allocates it before
        # forwarding /start (spec §3), and the daemon's /start accepts it (nelix-9a4.6). The forward
        # is PHASE-SPLIT (_forward_call) so the router can classify a failure as definite vs ambiguous.
        payload = {"executor": executor, "task": task, "cwd": cwd, "owner_id": self._owner}
        if model is not None:
            payload["model"] = model
        if session_id is not None:
            payload["session_id"] = session_id
        return self._forward_call("POST", "/start", payload, timeout=timeout)

    def health(self, timeout=10):
        """The generation's /health envelope (liveness + build-id `generation_id`). No owner and
        no session (spec §8/§10): the route carries neither, so the router can probe a generation's
        identity without impersonating a caller. `owner_id` is still required to CONSTRUCT this
        client, but is not sent on the wire for /health."""
        _, body = self._call("GET", "/health", timeout=timeout)
        return body

    def status(self, session_id=None, include_progress=False):
        # include_progress (Task 8): the query param is only ADDED when true, so a default call
        # is byte-for-byte the same request as before this option existed.
        params = {"owner_id": self._owner}
        if session_id:
            params["session_id"] = session_id
        if include_progress:
            params["include_progress"] = 1
        _, body = self._call("GET", "/status?" + urllib.parse.urlencode(params))
        return body

    def dialog(self, session_id, offset=0, limit=None):
        params = {"session_id": session_id, "offset": offset, "owner_id": self._owner}
        if limit is not None:
            params["limit"] = limit
        _, body = self._call("GET", "/dialog?" + urllib.parse.urlencode(params))
        return body

    def screen(self, session_id, raw=False, force=False):
        params = {"session_id": session_id, "owner_id": self._owner}
        if raw:
            params["raw"] = 1
        if force:
            params["force"] = 1
        _, body = self._call("GET", "/screen?" + urllib.parse.urlencode(params))
        return body

    def wait(self, session_id, after_seq=0, timeout=30):
        params = {"session_id": session_id, "after_seq": after_seq, "owner_id": self._owner}
        _, body = self._call("GET", "/wait?" + urllib.parse.urlencode(params), timeout=timeout)
        return body

    def respond(self, session_id, answer, decision_id=None):
        payload = {"session_id": session_id, "answer": answer, "owner_id": self._owner}
        if decision_id is not None:
            payload["decision_id"] = decision_id
        st, body = self._call("POST", "/respond", payload)
        return st == 200, body

    def stop(self, session_id):
        _, body = self._call("POST", "/stop", {"session_id": session_id,
                                               "owner_id": self._owner})
        return body

    def restart(self, session_id, *, new_session_id, force=False, owner_id=None):
        """POST /restart with a router-assigned new_session_id (nelix-9a4.4).
        owner_id is passed through from the caller explicitly for the router path;
        when omitted (direct RpcClient use), self._owner is used.
        Phase-split (_forward_call) so the router can classify failure as definite
        (ForwardConnectError) vs ambiguous (ForwardResponseError). Returns the body dict,
        same contract as start()/_forward_call."""
        payload = {"session_id": session_id, "force": force,
                   "new_session_id": new_session_id,
                   "owner_id": owner_id or self._owner}
        return self._forward_call("POST", "/restart", payload)
