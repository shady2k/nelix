import http.client
import json
import socket
import urllib.parse

try:
    from .daemon.transport import Transport     # package mode (hermes_plugins.nelix.rpc_client)
except ImportError:
    from daemon.transport import Transport      # top-level module mode (tests)


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
    def __init__(self, transport):
        self._t = transport

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

    def start(self, executor, task, cwd):
        _, body = self._call("POST", "/start",
                             {"executor": executor, "task": task, "cwd": cwd})
        return body

    def status(self, session_id=None, include_progress=False):
        # include_progress (Task 8): the query param is only ADDED when true, so a default call
        # is byte-for-byte the same request as before this option existed.
        params = {}
        if session_id:
            params["session_id"] = session_id
        if include_progress:
            params["include_progress"] = 1
        q = "?" + urllib.parse.urlencode(params) if params else ""
        _, body = self._call("GET", "/status" + q)
        return body

    def dialog(self, session_id, offset=0, limit=None):
        params = {"session_id": session_id, "offset": offset}
        if limit is not None:
            params["limit"] = limit
        _, body = self._call("GET", "/dialog?" + urllib.parse.urlencode(params))
        return body

    def screen(self, session_id, raw=False, force=False):
        params = {"session_id": session_id}
        if raw:
            params["raw"] = 1
        if force:
            params["force"] = 1
        _, body = self._call("GET", "/screen?" + urllib.parse.urlencode(params))
        return body

    def respond(self, session_id, answer, decision_id=None):
        payload = {"session_id": session_id, "answer": answer}
        if decision_id is not None:
            payload["decision_id"] = decision_id
        st, body = self._call("POST", "/respond", payload)
        return st == 200, body

    def stop(self, session_id):
        _, body = self._call("POST", "/stop", {"session_id": session_id})
        return body

    def restart(self, session_id, force=False):
        payload = {"session_id": session_id, "force": force}
        st, body = self._call("POST", "/restart", payload)
        return st == 200, body
