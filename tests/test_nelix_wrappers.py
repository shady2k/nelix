"""Executor-facing wrapper scripts `bin/nelix-question` / `bin/nelix-note` (Task 7): a Claude Code
executor posts an async question or a progress note to the daemon's `POST /message/<sid>` route
(Task 5) over the SAME env + transport the hook curl already uses (injected at spawn):
NELIX_HOOK_SOCK, NELIX_HOOK_SECRET, NELIX_SESSION. Stand up a real `make_server` on a unix socket
(mirrors tests/test_message_route.py's FakeManager) and run the scripts as subprocesses against it.
"""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from daemon.rpc_server import make_server
from daemon.transport import Transport

ROOT = Path(__file__).resolve().parents[1]
QUESTION = ROOT / "bin" / "nelix-question"
NOTE = ROOT / "bin" / "nelix-note"

SECRET = "sek"


class FakeSession:
    """Stands in for a real Session: exposes the per-session hook_secret shared by /hook and
    /message (see tests/test_message_route.py)."""

    def __init__(self, secret=SECRET):
        self.hook_secret = secret
        self.hooks = []

    def on_hook(self, ev):
        self.hooks.append(ev)

    def observe(self): pass
    def last_observed(self): return 0.0
    def orphan_marked_ts(self): return None
    def mark_orphaned(self, grace): pass


class FakeManager:
    """Minimal stand-in for SessionManager exercising only what /message needs: `get` (auth),
    `record_async_question`, `append_progress_note` (see tests/test_message_route.py)."""

    def __init__(self):
        self._sessions = {"s-11111111": FakeSession()}
        self._live = {"s-11111111"}
        self._qseq = {}
        self._pending = {}
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


@pytest.fixture
def unix_sock(tmp_path):
    """Short AF_UNIX socket path (<=103 chars incl. NUL) — see tests/test_rpc_server.py."""
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:8]
    p = f"/tmp/nxq{h}.sock"
    yield p
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass


@pytest.fixture
def server(unix_sock):
    import threading

    mgr = FakeManager()
    srv = make_server(mgr, Transport.unix(unix_sock))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield unix_sock
    srv.shutdown()
    srv.server_close()


def _env(sock, secret=SECRET, session="s-11111111", extra=None):
    e = {"PATH": "/usr/bin:/bin", "NELIX_HOOK_SOCK": sock,
         "NELIX_HOOK_SECRET": secret, "NELIX_SESSION": session}
    if extra:
        e.update(extra)
    return e


def _run(script, args, env, timeout=10):
    return subprocess.run([str(script)] + list(args), env=env, capture_output=True,
                          text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# nelix-question
# ---------------------------------------------------------------------------

def test_nelix_question_queued(server):
    r = _run(QUESTION, ["--question", "proceed with X?", "--continuation-plan", "keep coding Y"],
              env=_env(server))
    assert r.returncode == 0, r.stderr
    body = json.loads(r.stdout.strip())
    assert body["status"] == "queued"
    assert body["id"] == "q_1"


def test_nelix_question_optional_fields_included(server):
    r = _run(QUESTION, ["--question", "a?", "--continuation-plan", "coding",
                        "--assumption", "yes by default", "--impact-if-wrong", "revert the change"],
              env=_env(server))
    assert r.returncode == 0, r.stderr
    body = json.loads(r.stdout.strip())
    assert body["status"] == "queued"


def test_nelix_question_already_pending_nonzero_exit(server):
    _run(QUESTION, ["--question", "first?", "--continuation-plan", "coding"], env=_env(server))
    r = _run(QUESTION, ["--question", "second?", "--continuation-plan", "coding"], env=_env(server))
    assert r.returncode != 0
    body = json.loads(r.stdout.strip())
    assert body["status"] == "already_pending"


def test_nelix_question_missing_env_exits_2(tmp_path):
    r = _run(QUESTION, ["--question", "a?", "--continuation-plan", "coding"],
              env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 2
    assert "not running under a nelix session" in r.stderr


def test_nelix_question_missing_required_flag_errors(server):
    r = _run(QUESTION, ["--question", "a?"], env=_env(server))
    assert r.returncode != 0


# ---------------------------------------------------------------------------
# nelix-note
# ---------------------------------------------------------------------------

def test_nelix_note_recorded(server):
    r = _run(NOTE, ["--summary", "step 1 done"], env=_env(server))
    assert r.returncode == 0, r.stderr
    body = json.loads(r.stdout.strip())
    assert body["status"] == "recorded"
    assert body["progress_seq"] == 1


def test_nelix_note_progress_seq_increments(server):
    _run(NOTE, ["--summary", "one"], env=_env(server))
    r = _run(NOTE, ["--summary", "two"], env=_env(server))
    body = json.loads(r.stdout.strip())
    assert r.returncode == 0
    assert body["progress_seq"] == 2


def test_nelix_note_optional_details_included(server):
    r = _run(NOTE, ["--summary", "step", "--details", "longer explanation"], env=_env(server))
    assert r.returncode == 0, r.stderr
    body = json.loads(r.stdout.strip())
    assert body["status"] == "recorded"


def test_nelix_note_missing_env_exits_2(tmp_path):
    r = _run(NOTE, ["--summary", "x"], env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 2
    assert "not running under a nelix session" in r.stderr


def test_nelix_note_wrong_secret_nonzero_exit(server):
    r = _run(NOTE, ["--summary", "x"], env=_env(server, secret="wrong"))
    assert r.returncode != 0
