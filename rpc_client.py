import json
import urllib.error
import urllib.request


class RpcClient:
    def __init__(self, base, token):
        self._base = base.rstrip("/")
        self._token = token

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self._base + path, data=data, method=method,
                                     headers={"X-Nelix-Token": self._token,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def start(self, executor, task):
        _, body = self._call("POST", "/start", {"executor": executor, "task": task})
        return body

    def status(self, session_id=None):
        q = f"?session_id={session_id}" if session_id else ""
        _, body = self._call("GET", "/status" + q)
        return body

    def dialog(self, session_id, turn=None, offset=0, limit=None):
        q = f"?session_id={session_id}&offset={offset}"
        if turn is not None:
            q += f"&turn={turn}"
        if limit is not None:
            q += f"&limit={limit}"
        _, body = self._call("GET", "/dialog" + q)
        return body

    def respond(self, session_id, event_id, answer):
        st, body = self._call("POST", "/respond",
                              {"session_id": session_id, "event_id": event_id, "answer": answer})
        return st == 200, body

    def stop(self, session_id):
        _, body = self._call("POST", "/stop", {"session_id": session_id})
        return body
