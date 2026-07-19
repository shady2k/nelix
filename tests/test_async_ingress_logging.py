"""nelix-wz6: the async-message channel (executor -> orchestrator) had NO ingress-side audit
logging — a live run's daemon log recorded only `async_question_resolved`, so notes recorded, a
question's arrival, and the wake published for it were all inferred from absence. These tests drive
the incoming half through a real Logger and assert the structured records fire (same one-line-JSON
shape as `async_question_resolved`):
  - `note_recorded`        (progress_seq + bounded summary) when a non-waking note is appended,
  - `async_question_asked` (question_id) when a question is received,
  - `wake_published`       (kind + seq) at the question's ONE EventQueue publish (the sole
                            async-channel wake site — notes deliberately never publish).
Observability only: behavior (slot install, wake, snapshot) is unchanged.
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from daemon.clock import FakeClock              # noqa: E402
from daemon.config import MAX_SUMMARY_LEN       # noqa: E402
from daemon.dialog import Dialog                # noqa: E402
from daemon.drivers.claude import ClaudeDriver  # noqa: E402
from daemon.events import EventQueue            # noqa: E402
from daemon.messages import AsyncQuestion, ProgressNote  # noqa: E402
from daemon.obs import Logger                   # noqa: E402
from daemon.session import Session              # noqa: E402

from tests.test_async_question import Spec, FakeHandle  # noqa: E402


def _records(buf):
    return [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]


def _events(buf, name):
    return [r for r in _records(buf) if r["event"] == name]


def _busy_session(tmp_path):
    """A working session (busy, no decision) with a real StringIO-backed Logger so the ingress
    records are captured. Mirrors test_async_question._make_session but injects the logger."""
    buf = io.StringIO()
    log = Logger(level="debug", stream=buf, audit_stream=buf)
    ev = EventQueue()
    clock = FakeClock(0.0)
    sess = Session("s1", "demo", ClaudeDriver(), None, Spec(), ev, logger=log, clock=clock)
    sess._handle = FakeHandle(["compiling…", "compiling…"], stop=sess._stop, clock=clock)
    sess._dialog = Dialog(tmp_path / "s1", tail_lines=Spec.tail_lines,
                          spool_max_bytes=Spec.spool_max_bytes)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "delivered"
    sess._loop()
    assert sess._decision is None and sess._state == "busy"
    return sess, buf


def test_note_recorded_logged_with_seq_and_bounded_summary(tmp_path):
    sess, buf = _busy_session(tmp_path)
    seq = sess.append_progress_note(ProgressNote("compiled module A", None))
    recs = _events(buf, "note_recorded")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["session_id"] == "s1"
    assert rec["progress_seq"] == seq == 1
    assert rec["summary"] == "compiled module A"


def test_note_recorded_summary_is_bounded(tmp_path):
    sess, buf = _busy_session(tmp_path)
    long_summary = "x" * (MAX_SUMMARY_LEN + 500)
    sess.append_progress_note(ProgressNote(long_summary, None))
    rec = _events(buf, "note_recorded")[0]
    assert len(rec["summary"]) <= MAX_SUMMARY_LEN     # config body cap reused; never unbounded text


def test_async_question_asked_logged_with_question_id(tmp_path):
    sess, buf = _busy_session(tmp_path)
    qid, err = sess.record_async_question(AsyncQuestion("a or b?", "keep coding", None, None))
    assert err is None
    recs = _events(buf, "async_question_asked")
    assert len(recs) == 1
    assert recs[0]["session_id"] == "s1"
    assert recs[0]["question_id"] == qid == "q_1"


def test_wake_published_logged_with_kind_and_seq(tmp_path):
    sess, buf = _busy_session(tmp_path)
    qid, _ = sess.record_async_question(AsyncQuestion("a or b?", "keep coding", None, None))
    recs = _events(buf, "wake_published")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["session_id"] == "s1"
    assert rec["kind"] == "async_question"
    assert rec["seq"] == sess._events.latest_seq()    # the published wake event's seq
    assert rec["question_id"] == qid


def test_note_does_not_publish_wake(tmp_path):
    # Notes are non-waking: appending one must emit note_recorded but NEVER a wake_published
    # (the whole point of nelix-note — it bypasses the EventQueue). Guards against a regression
    # that would trip an armed nelix-wait long-poll (the phantom pre-delivery class, 92a0dc6).
    sess, buf = _busy_session(tmp_path)
    sess.append_progress_note(ProgressNote("still going", None))
    assert _events(buf, "note_recorded")
    assert _events(buf, "wake_published") == []


def test_already_pending_question_does_not_relog_asked(tmp_path):
    # A duplicate/retried question while one is already outstanding is a no-op (same id back, no new
    # wake) — it must NOT emit a second async_question_asked/wake_published pair.
    sess, buf = _busy_session(tmp_path)
    sess.record_async_question(AsyncQuestion("q1", "c", None, None))
    qid2, err2 = sess.record_async_question(AsyncQuestion("q2", "c", None, None))
    assert qid2 is None and err2["id"] == "q_1"
    assert len(_events(buf, "async_question_asked")) == 1
    assert len(_events(buf, "wake_published")) == 1
