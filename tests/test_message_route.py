"""POST /message/<sid> route (Task 5): an executor posts an async `question` (waking, queued for a
later reply) or `note` (non-waking progress update) without blocking on a response. Same per-session
secret as /hook (X-Nelix-Hook-Secret), authenticated identically (fail closed for unknown session /
missing / bad secret — no existence oracle), but a SEPARATE rate-limit bucket: message spam must
never starve hook delivery (test_message_limiter_separate_from_hooks is the regression lock for
that invariant).
"""
import http.client
import json
import threading
from urllib.parse import urlparse

import pytest

from daemon.rpc_server import (
    make_server,
    _HookRateLimiter,
    _HOOK_RATE_CAPACITY as MSG_LIMIT,
    _HOOK_RATE_REFILL as MSG_REFILL,
)
from daemon.transport import Transport

SECRET = "sek"
TOKEN = "t"


class FakeSession:
    """Stands in for a real Session: exposes the per-session hook_secret shared by /hook and
    /message, and records every HookEvent handed to on_hook (used by the cross-route limiter test)."""

    def __init__(self, secret=SECRET):
        self.hook_secret = secret
        self.hooks = []

    def on_hook(self, ev):
        self.hooks.append(ev)


class FakeManager:
    """A minimal stand-in for SessionManager exercising only what the route needs: `get` (for auth),
    `record_async_question`, and `append_progress_note` — with the same return shapes the real
    Manager/Session methods use (see daemon/manager.py + daemon/session.py)."""

    def __init__(self):
        # "s1" is a normal live session. "s-race" is authenticatable (present for `get`, so /message
        # auth succeeds) but absent from `_live` — it stands in for the rare post-auth race the brief
        # documents (session freed between the auth lookup and the state-mutating call), so the two
        # manager methods below hit their unknown_session branch even though auth passed.
        self._sessions = {"s1": FakeSession(), "s-race": FakeSession()}
        self._live = {"s1"}
        self._qseq = {}
        self._pending = {}      # sid -> {"id":..., "question":...}
        self._progress_seq = {}

    def get(self, sid):
        return self._sessions.get(sid)

    def record_async_question(self, sid, q):
        if sid not in self._live:
            return None, {"error": "unknown_session"}
        pending = self._pending.get(sid)
        if pending is not None:
            return None, {"id": pending["id"], "question": pending["question"][:200]}
        n = self._qseq.get(sid, 0) + 1
        self._qseq[sid] = n
        qid = f"q_{n}"
        self._pending[sid] = {"id": qid, "question": q.question}
        return qid, None

    def append_progress_note(self, sid, note):
        if sid not in self._live:
            return None
        n = self._progress_seq.get(sid, 0) + 1
        self._progress_seq[sid] = n
        return n


def _start_server(**make_server_kwargs):
    """Boot a real make_server on an ephemeral TCP port in a daemon thread; return (srv, base_url)."""
    mgr = FakeManager()
    srv = make_server(mgr, Transport.tcp("127.0.0.1", 0, TOKEN), **make_server_kwargs)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    return srv, base


@pytest.fixture
def server():
    srv, base = _start_server()
    yield base
    srv.shutdown()


@pytest.fixture
def frozen_clock_server():
    """Like `server`, but the rate-limit buckets read a FROZEN clock (time never advances). Bucket
    exhaustion is then decided purely by request COUNT, so a busy machine's wall-clock refill can
    never mask it — the rate-limit tests become deterministic under load (nelix-3s3). make_server
    just threads the injectable clock the token bucket already accepts down into both limiters."""
    srv, base = _start_server(clock=lambda: 0.0)
    yield base
    srv.shutdown()


def _post(base, path, body, *, secret_header, secret):
    u = urlparse(base)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    hdrs = {"X-Nelix-Token": TOKEN}
    if secret is not None:
        hdrs[secret_header] = secret
    data = json.dumps(body).encode()
    c.request("POST", path, body=data, headers=hdrs)
    r = c.getresponse()
    st = r.status
    raw = r.read()
    c.close()
    parsed = json.loads(raw) if raw else None
    return st, parsed


def post(base, path, body, secret=SECRET):
    return _post(base, path, body, secret_header="X-Nelix-Hook-Secret", secret=secret)


def post_hook(base, path, body, secret=SECRET):
    return _post(base, path, body, secret_header="X-Nelix-Hook-Secret", secret=secret)


def test_unauthorized_without_secret(server):
    st, _ = post(server, "/message/s1", {"kind": "note", "summary": "x"}, secret=None)
    assert st == 401


def test_unauthorized_wrong_secret(server):
    st, _ = post(server, "/message/s1", {"kind": "note", "summary": "x"}, secret="wrong")
    assert st == 401


def test_unauthorized_unknown_session(server):
    # No existence oracle: an unknown session 401s exactly like a bad secret on a known one.
    st, _ = post(server, "/message/no-such-session", {"kind": "note", "summary": "x"})
    assert st == 401


def test_note_recorded(server):
    st, body = post(server, "/message/s1", {"kind": "note", "summary": "step 2"})
    assert st == 200 and body["status"] == "recorded" and body["progress_seq"] == 1


def test_note_progress_seq_increments(server):
    post(server, "/message/s1", {"kind": "note", "summary": "one"})
    st, body = post(server, "/message/s1", {"kind": "note", "summary": "two"})
    assert st == 200 and body["progress_seq"] == 2


def test_question_queued(server):
    st, body = post(server, "/message/s1",
                    {"kind": "question", "question": "a?", "continuation_plan": "coding"})
    assert st == 200 and body["status"] == "queued" and body["id"] == "q_1"


def test_question_already_pending_409(server):
    post(server, "/message/s1",
         {"kind": "question", "question": "first?", "continuation_plan": "coding"})
    st, body = post(server, "/message/s1",
                     {"kind": "question", "question": "second?", "continuation_plan": "coding"})
    assert st == 409
    assert body["status"] == "already_pending"
    assert body["pending"] == {"id": "q_1", "question": "first?"}


def test_question_missing_continuation_plan_400(server):
    st, body = post(server, "/message/s1", {"kind": "question", "question": "a?"})
    assert st == 400 and "continuation_plan" in body["error"]


def test_note_missing_summary_400(server):
    st, body = post(server, "/message/s1", {"kind": "note"})
    assert st == 400 and "summary" in body["error"]


def test_unknown_kind_400(server):
    st, body = post(server, "/message/s1", {"kind": "bogus"})
    assert st == 400


def test_note_unknown_session_race_404(server):
    # Auth already 401s a truly-unknown session (no existence oracle); this exercises the
    # documented rare post-auth race — a session that authenticates but is gone by the time the
    # manager tries to mutate its state — which the route must map to 404, not 401 or 500.
    st, body = post(server, "/message/s-race", {"kind": "note", "summary": "x"})
    assert st == 404 and body["error"] == "unknown_session"


def test_question_unknown_session_race_404(server):
    st, body = post(server, "/message/s-race",
                     {"kind": "question", "question": "a?", "continuation_plan": "coding"})
    assert st == 404 and body["error"] == "unknown_session"


def test_message_limiter_separate_from_hooks(frozen_clock_server):
    # Frozen clock ⇒ the message bucket drains by COUNT (no wall-clock refill), so this exhaustion
    # step is deterministic under load.
    for _ in range(MSG_LIMIT + 2):
        post(frozen_clock_server, "/message/s1", {"kind": "note", "summary": "x"})
    # ... a /hook POST for the SAME session must still pass: message spam must never starve hooks
    # (a SEPARATE bucket, never drained here).
    st, _ = post_hook(frozen_clock_server, "/hook/s1", {"hook_event_name": "Stop"})
    assert st == 204


def test_message_bucket_exhausted_returns_429(frozen_clock_server):
    # nelix-3s3: the message-plane token bucket has capacity MSG_LIMIT and refills over wall-clock
    # time. The original test drove MSG_LIMIT+2 real HTTP POSTs and relied on them completing before
    # the bucket refilled a whole token — a busy machine could refill enough between POSTs to let the
    # last one through (200, not 429), flaking the gate. `frozen_clock_server` freezes the bucket's
    # clock so NO refill can occur: MSG_LIMIT tokens drain, then every further POST is denied. The
    # outcome now depends only on request COUNT, so timing pressure can never flake it.
    last = None
    for _ in range(MSG_LIMIT + 2):
        last, _ = post(frozen_clock_server, "/message/s1", {"kind": "note", "summary": "x"})
    assert last == 429


def test_message_bucket_exhaustion_is_clock_driven():
    # Root-cause regression lock for nelix-3s3 (unit-level, no wall-clock): the token bucket's
    # exhaustion is decided by its injected clock, which is exactly WHY the route test flaked on a
    # real clock and why freezing it fixes the flake.
    #  - FROZEN clock: no refill ⇒ exactly MSG_LIMIT allowed, then denied forever (deterministic 429).
    #  - ADVANCING clock (a busy machine: enough wall-clock elapses between POSTs to refill >1 token
    #    per drain): the bucket never fully exhausts, so a post past MSG_LIMIT is still allowed —
    #    that is the "429 not returned" flake the route test used to be exposed to.
    sid = "s1"
    frozen = _HookRateLimiter(clock=lambda: 0.0)
    verdicts = [frozen.allow(sid) for _ in range(MSG_LIMIT + 2)]
    assert verdicts[:MSG_LIMIT] == [True] * MSG_LIMIT      # first MSG_LIMIT drain the bucket
    assert verdicts[MSG_LIMIT:] == [False, False]          # everything past capacity is denied

    ticks = {"t": 0.0}
    def advancing():
        # 2/MSG_REFILL seconds per call refills ~2 tokens for every 1 drained — the bucket climbs
        # back to capacity instead of emptying, mirroring a machine slow enough that more than a
        # full token refills between POSTs.
        ticks["t"] += 2.0 / MSG_REFILL
        return ticks["t"]
    refilling = _HookRateLimiter(clock=advancing)
    assert refilling.allow(sid) is True                    # refills faster than it drains ...
    for _ in range(MSG_LIMIT + 5):
        refilling.allow(sid)
    assert refilling.allow(sid) is True                    # ... so it is still not exhausted (the flake)


def test_message_oversized_body_is_413(server):
    # M1 (final whole-branch review): the route consumes config.MSG_MAX_BODY (no separate magic
    # number) — a claimed Content-Length past that cap 413s, mirroring /hook's tight body cap.
    from daemon.config import MSG_MAX_BODY
    u = urlparse(server)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    c.putrequest("POST", "/message/s1")
    c.putheader("X-Nelix-Token", TOKEN)
    c.putheader("X-Nelix-Hook-Secret", SECRET)
    c.putheader("Content-Length", str(MSG_MAX_BODY + 1))   # claim past the cap ...
    c.endheaders()                                         # ... but send no body
    r = c.getresponse(); st = r.status; r.read(); c.close()
    assert st == 413
