"""nelix-jwv: observability overhaul. The daemon log must make the three known failure classes
(silent respond-stall, re-mint flap, respond-fail) diagnosable FROM THE LOG ALONE. These tests
drive each path and assert the structured record fires with the right `event` + key fields, in the
existing one-line-JSON shape (info for lifecycle, debug for high-volume per-tick detail).

The belief engine stays pure: it surfaces suppression/post-submit rationale through a side-channel
note buffer (drain_notes) — NEVER on tick()'s action list (engine action-equality tests rely on it).
"""
import io
import json

from daemon.obs import Logger
from daemon.belief import BeliefEngine, Note
from daemon.clock import FakeClock
from daemon.observation import Observation
from daemon.config import BeliefConfig

from tests.test_session import (_session, respond_via_submit_monitor, _BOX, _WORKING, _echo,
                          WedgedWriteHandle)
from tests.test_belief_engine import busy, idle, CTX


def _log_buf():
    buf = io.StringIO()
    return buf, Logger(level="debug", stream=buf, audit_stream=buf)


def _records(buf):
    return [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]


def _by_event(buf):
    out = {}
    for r in _records(buf):
        out.setdefault(r["event"], r)
    return out


# ---------------------------------------------------------------------------
# Gap 1: respond lifecycle (mirror START's delivery_attempt -> delivery_confirmed | delivery_failed)
# ---------------------------------------------------------------------------

def test_respond_success_logs_attempt_submitted_confirmed(tmp_path):
    buf, log = _log_buf()
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._log = log
    sess._loop()
    did = sess._decision["decision_id"]
    sess._stop.clear()                                # a real respond runs while the monitor is live
    out = respond_via_submit_monitor(sess, "do the next thing", did, [_BOX, _WORKING])
    assert out.status == "resumed"
    by = _by_event(buf)
    assert "respond_attempt" in by
    assert by["respond_attempt"]["prompt_kind"] == "free_text"
    assert by["respond_attempt"]["decision_id"] == did
    assert by["respond_attempt"]["answer_chars"] == len("do the next thing")
    assert "respond_submitted" in by
    assert "respond_confirmed" in by
    assert by["respond_confirmed"]["level"] == "info"          # the lifecycle line is info-visible
    assert by["respond_confirmed"]["decision_id"] == did
    assert "respond_failed" not in by


def test_respond_attempt_logs_redacted_answer(tmp_path):
    # nelix-eea: respond_attempt logged only answer_chars (a count) -> the literal answer (Enter vs
    # `1` vs an option-id) was unrecoverable from the log (the "where did the 1 come from" incident,
    # s-9c0b6eeb). Log the answer itself, run through the obs free-text redactor (answer is in
    # _FREE_TEXT_FIELDS) so a secret pattern is MASKED but the SHAPE stays visible for forensics.
    buf, log = _log_buf()
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._log = log
    sess._loop()
    sess._stop.clear()                                # a real respond runs while the monitor is live
    answer = "deploy sk-livesecret1234567890abcd now"    # a secret token embedded in shaped free text
    out = respond_via_submit_monitor(sess, answer, sess._decision["decision_id"], [_BOX, _WORKING])
    assert out.status == "resumed"
    rec = _by_event(buf)["respond_attempt"]
    assert "answer" in rec                                    # the answer text is now logged, not just a count
    assert "sk-livesecret1234567890abcd" not in rec["answer"] # the secret pattern is masked
    assert "***" in rec["answer"]                            # ... masked, not silently dropped
    assert rec["answer"].startswith("deploy ") and rec["answer"].endswith(" now")  # shape survives
    assert rec["answer_chars"] == len(answer)                # the count is unchanged (both coexist)


def test_respond_submit_unconfirmed_logs_respond_failed(tmp_path):
    # Free-text Enter dropped: the answer is stranded in the box -> respond_failed(submit_unconfirmed).
    buf, log = _log_buf()
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._log = log
    sess._loop()
    sess._stop.clear()
    out = respond_via_submit_monitor(sess, "do the next thing", sess._decision["decision_id"],
                                     [_BOX, _echo("do the next thing")])
    assert out.status == "respond_failed"
    by = _by_event(buf)
    assert "respond_failed" in by
    assert by["respond_failed"]["level"] == "warning"
    assert by["respond_failed"]["reason"] == "submit_unconfirmed"
    assert "respond_confirmed" not in by


def test_respond_write_timeout_logs_respond_failed(tmp_path):
    # Executor stopped draining stdin: the bounded write times out -> respond_failed(write_unconfirmed).
    buf, log = _log_buf()
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._log = log
    sess._loop()
    sess._stop.clear()
    sess._handle = WedgedWriteHandle()
    out = respond_via_submit_monitor(sess, "1", sess._decision["decision_id"], [_BOX])
    assert out.status == "write_timeout"
    by = _by_event(buf)
    assert "respond_attempt" in by                            # the attempt is recorded before the write
    assert "respond_failed" in by
    assert by["respond_failed"]["level"] == "warning"
    assert by["respond_failed"]["reason"] == "write_unconfirmed"


# ---------------------------------------------------------------------------
# Gap 2: belief suppression / post-submit rationale (pure engine -> note buffer)
# ---------------------------------------------------------------------------

def test_engine_notes_are_off_the_action_list():
    # The diagnostic notes must NEVER ride on tick()'s returned actions (engine tests assert == []).
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    assert e.tick(busy(), CTX) == []
    clk.advance(0.1)
    assert e.tick(idle(), CTX) == []                          # not settled: no action, but a note
    notes = e.drain_notes()
    assert notes and all(isinstance(n, Note) for n in notes)
    assert e.drain_notes() == []                              # drain clears the buffer


def test_engine_suppressed_not_settled():
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.tick(busy(), CTX)
    clk.advance(0.1)
    e.tick(idle(), CTX)                                       # idle edge, < idle_confirm_window
    notes = e.drain_notes()
    assert any(n.event == "belief_suppressed" and n.fields.get("reason") == "not_settled"
               for n in notes)


def test_engine_post_submit_armed_and_echo_suppressed():
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.on_submit("answer")                                     # arms post-submit suppression
    echo = Observation(prompt_kind="free_text", semantic_fp="idle", content_fp="c",
                       submitted_echo_present=True,
                       affordances=frozenset({"accepts_text_input"}))
    clk.advance(0.1)
    e.tick(echo, CTX)                                         # our echo still in the box -> suppress
    notes = e.drain_notes()
    assert any(n.event == "post_submit_armed" for n in notes)
    assert any(n.event == "belief_suppressed"
               and n.fields.get("reason") == "submitted_echo_present" for n in notes)


def test_engine_post_submit_cleared_on_real_output():
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.on_submit("answer")
    clk.advance(0.1)
    e.tick(Observation(prompt_kind="none", semantic_fp="x", content_fp="c0"), CTX)  # baseline
    clk.advance(0.1)
    e.tick(Observation(prompt_kind="none", semantic_fp="x", content_fp="c1"), CTX)  # real output
    notes = e.drain_notes()
    assert any(n.event == "post_submit_cleared" and n.fields.get("reason") == "real_output"
               for n in notes)


def test_engine_suppressed_is_edge_triggered_not_per_tick():
    # A persistent suppression reason logs ONCE (the edge), not every tick — signal, not noise.
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.tick(busy(), CTX)
    e.drain_notes()
    suppressed = 0
    for _ in range(4):                                        # four idle ticks, all still not settled
        clk.advance(0.05)
        e.tick(idle(), CTX)
        suppressed += sum(1 for n in e.drain_notes()
                          if n.event == "belief_suppressed" and n.fields.get("reason") == "not_settled")
    assert suppressed == 1                                    # one edge record for the whole episode


def test_belief_notes_reach_the_daemon_log(tmp_path):
    # Wiring: the engine's notes are drained by Session and written to the structured log at debug.
    buf, log = _log_buf()
    box = "Ready — what next?\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"
    sess, ev = _session(tmp_path, ["working esc to interrupt", box, box, box])
    sess._log = log
    sess._loop()
    sup = [r for r in _records(buf) if r["event"] == "belief_suppressed"]
    assert any(r["reason"] == "not_settled" for r in sup)
    assert all(r["level"] == "debug" for r in sup)            # high-volume detail stays at debug


# ---------------------------------------------------------------------------
# Gap 3: session/EventQueue decision lifecycle (published / answered / superseded)
# ---------------------------------------------------------------------------

def test_decision_published_logged(tmp_path):
    buf, log = _log_buf()
    box = "Ready — what next?\n❯ \n⏵⏵ ask mode (shift+tab to cycle)"
    sess, ev = _session(tmp_path, ["working esc to interrupt", box, box, box])
    sess._log = log
    sess._loop()
    by = _by_event(buf)
    assert "decision_published" in by
    assert by["decision_published"]["level"] == "info"
    assert by["decision_published"]["decision_id"] == sess._decision["decision_id"]
    assert by["decision_published"]["kind"] == "waiting_for_user"


def test_decision_superseded_logged(tmp_path):
    buf, log = _log_buf()
    sess, ev = _session(tmp_path, ["screen"])
    sess._log = log
    sess._publish("waiting_for_user", hint=None, hung=False, requires_response=True, decision_key="k-A")
    a_id = sess._decision["decision_id"]
    sess._publish("waiting_for_user", hint=None, hung=False, requires_response=True, decision_key="k-B")
    b_id = sess._decision["decision_id"]
    sup = [r for r in _records(buf) if r["event"] == "decision_superseded"]
    assert any(r["decision_id"] == a_id and r.get("superseded_by") == b_id for r in sup)
    assert all(r["level"] == "info" for r in sup)


def test_decision_answered_logged(tmp_path):
    buf, log = _log_buf()
    sess, ev = _session(tmp_path, ["working esc to interrupt", _BOX, _BOX, _BOX])
    sess._log = log
    sess._loop()
    sess._stop.clear()                                # a real respond runs while the monitor is live
    did = sess._decision["decision_id"]
    out = respond_via_submit_monitor(sess, "1", did, [_BOX, _WORKING])
    assert out.status == "resumed"
    by = _by_event(buf)
    assert "decision_answered" in by
    assert by["decision_answered"]["level"] == "info"
    assert by["decision_answered"]["decision_id"] == did
