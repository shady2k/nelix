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


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


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

    def _call(self, method, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self._t.kind == "tcp":
            headers["X-Nelix-Token"] = self._t.token
        conn = self._conn(timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def start(self, executor, task, cwd, model=None):
        # model (nelix-9k0): the key is only ADDED when provided, so a default (no-model) call is
        # byte-for-byte the same request as before this option existed (mirrors status's include_progress).
        payload = {"executor": executor, "task": task, "cwd": cwd, "owner_id": self._owner}
        if model is not None:
            payload["model"] = model
        _, body = self._call("POST", "/start", payload)
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

    def restart(self, session_id, force=False):
        payload = {"session_id": session_id, "force": force, "owner_id": self._owner}
        st, body = self._call("POST", "/restart", payload)
        return st == 200, body
