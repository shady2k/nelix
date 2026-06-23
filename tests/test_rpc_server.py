import json
import threading
import urllib.error
import urllib.request

from daemon.events import EventQueue
from daemon.rpc_server import make_server


class FakeSession:
    def __init__(self):
        self.q = EventQueue()
        self.started = None
        self.responded = []

    def start(self, task):
        self.started = task

    def wait_event(self, after_seq, timeout):
        return self.q.latest_after(after_seq)

    def respond(self, event_id, answer):
        self.responded.append((event_id, answer))
        return True

    def snapshot(self):
        return {"state": "working"}


def _req(method, url, token="t", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={"X-Nelix-Token": token})
    with urllib.request.urlopen(r, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def test_rpc_roundtrip():
    sess = FakeSession()
    srv = make_server(sess, token="t", port=8766)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:8766"
    try:
        st, _ = _req("POST", base + "/start", body={"task": "hi"})
        assert st == 200 and sess.started == "hi"
        sess.q.publish("waiting_for_user", "y/n?", "waiting_for_user")
        _, body = _req("GET", base + "/wait?after_seq=0")
        eid = body["event"]["event_id"]
        st, _ = _req("POST", base + "/respond", body={"event_id": eid, "answer": "yes"})
        assert st == 200 and sess.responded == [(eid, "yes")]
    finally:
        srv.shutdown()


def test_rpc_requires_token():
    srv = make_server(FakeSession(), token="t", port=8767)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = urllib.request.Request("http://127.0.0.1:8767/status", method="GET")
        try:
            urllib.request.urlopen(r, timeout=5)
            assert False, "expected 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        srv.shutdown()
