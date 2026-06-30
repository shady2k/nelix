"""Tier-3 session-loop replay oracles (nelix-5gc Task 6) — GLUE that only the real Session owns.

Tiers 1-2 drive renderer + ClaudeDriver + BeliefEngine directly and BYPASS daemon.session.Session.
These two oracles replay REAL captures THROUGH the real Session (via tests/_session_replay.py's
RawReplayHandle, the same in-process handle pattern tests/test_session.py uses) to assert glue the
lower tiers cannot reach:

  I6b  — modal respond ROUTING: a real numbered-modal capture drives Session to publish a
          modal_choice decision; Session.respond(valid id) must actuate driver.select_option
          (digit+CR as ONE PTY write) and record the option LABEL; respond(invalid id) is rejected
          and the decision stays pending.  Real-capture version of the synthetic
          test_session.py::test_respond_to_modal_routes_to_select_option.

  delivery — the real paste-echo capture drives Session's DELIVERY path; the task is confirmed
          delivered exactly ONCE (typed once as a bracketed paste, Enter once) off the REAL
          '❯\\xa0[Pasted text #1]' placeholder, and exactly ONE terminal event with the correct
          terminal_kind is published.  test_replay_trail.py bypasses all of this.

Each oracle is non-vacuous: it asserts the modal decision / delivery window was ACTUALLY reached
before asserting the routing / once-only behaviour.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._session_replay import (   # noqa: E402
    replay_session, delivery_run, capture_frames, raw_frames,
)


# ─────────────────────────────────────────────────────────────────────────────
# I6b — modal respond routing over a REAL numbered-modal capture (s-beb967e9)
# ─────────────────────────────────────────────────────────────────────────────
# s-beb967e9 is a real claude T6/T7 run, stopped ON the agent's numbered "ask the user" menu.
# Replaying its byte stream ends on a modal_choice frame (options 1/2/3); the SAME capture is the
# source for test_replay_trail.py::test_t7_menu_surfaces_as_modal_choice — but that test stops at the
# engine and never exercises Session.respond's affordance-aware routing (the F2 fix, commit 8ecb2f5).

def test_i6b_modal_respond_routes_to_select_option_over_real_capture(tmp_path):
    """RED evidence (local mutation, NOT committed — reverts the 8ecb2f5 affordance-aware routing):
        in daemon/session.py::respond, change
            is_modal = decision.get("prompt_kind") in ("modal_choice", "permission_choice")
        to
            is_modal = False
        Then run this test.
    RED result: respond('9') is no longer rejected (status 'resumed', not 'invalid_option') and
    respond('1') takes the free-text path -> writes ['1', '\\r'] (submit_text + separate Enter), so
    '1\\r' is NOT in writes and '1' IS -> both routing assertions FAIL.  Restore with
    `git checkout daemon/session.py`.
    GREEN: respond('9') -> invalid_option (nothing typed); respond('1') -> driver.select_option ->
    one combined '1\\r' write + the option LABEL recorded.
    """
    # Drive the real Session loop over the full real capture; pad the final modal frame so the
    # engine's idle_confirm_window settles and the decision actually publishes (mirrors the synthetic
    # test repeating the modal frame).
    sess, ev = replay_session(tmp_path, capture_frames("s-beb967e9.capture"), pad_last=4)
    sess._loop()

    # NON-VACUITY: the modal decision was ACTUALLY published over the real stream (not a 0==0 pass).
    dec = sess.snapshot().get("decision")
    assert dec is not None, "driving the real T7 capture must leave a pending decision"
    assert dec["kind"] == "waiting_for_user" and dec["prompt_kind"] == "modal_choice", (
        f"the T7 numbered menu must surface as a modal_choice (got {dec.get('prompt_kind')!r})")
    assert [o["id"] for o in dec["options"]] == ["1", "2", "3"]
    assert ev.pending("s1") is not None
    did = dec["decision_id"]

    # Invalid option id -> REJECTED before claiming; decision stays pending; NOTHING typed into the
    # menu (mirrors session.py: `if is_modal and clean not in {o["id"] ...}: invalid_option`).
    writes_before = list(sess._handle.writes)
    bad = sess.respond("9", decision_id=did)
    assert bad.status == "invalid_option"
    assert sess._decision is not None and sess._decision["decision_id"] == did   # still pending
    assert ev.pending("s1") is not None
    assert sess._handle.writes == writes_before                                  # no keys sent

    # Valid option id -> the DRIVER performs the selection: select_option emits the digit + submit key
    # as ONE sequence ('1\r'), NOT the free-text type-then-Enter path (which would be '1' then '\r').
    out = sess.respond("1", decision_id=did)
    assert out.status == "resumed"
    assert "1\r" in sess._handle.writes                 # select_option: digit+CR, one write
    assert "1" not in sess._handle.writes               # NOT the free-text two-write path
    # The chosen option's LABEL (not the bare id) is recorded in the transcript.
    assert "Enrich all three" in sess._dialog.page()["text"]
    assert ev.pending("s1") is None                     # answered -> cleared


# ─────────────────────────────────────────────────────────────────────────────
# delivery — confirm-once + terminal publication over the REAL paste-echo capture (s-b8a30317)
# ─────────────────────────────────────────────────────────────────────────────
# s-b8a30317-delivery is the real paste-delivery byte stream: an input box appears, then the pasted
# task collapses to '❯\xa0[Pasted text #1]' (a NBSP between ❯ and the placeholder — the exact bytes
# the driver's _PASTED_TEXT regex was hardened for, "verified from a live capture (s-b8a30317)").
# The synthetic delivery tests (test_delivery_confirms_when_claude_collapses_paste) FABRICATE a
# regular-space '❯ [Pasted text #1]' echo; this oracle confirms delivery off the REAL NBSP frame.

def test_delivery_confirms_once_and_publishes_terminal_over_real_capture(tmp_path, monkeypatch):
    """RED evidence (local mutation, NOT committed — the NBSP-delivery bug class the memory cites):
        in daemon/drivers/claude.py change the _PASTED_TEXT char class
            _PASTED_TEXT = re.compile("\\u276f[ \\t\\xa0]*" + r"\\[Pasted text #\\d+\\]")
        to drop the NBSP
            _PASTED_TEXT = re.compile("\\u276f[ \\t]*"   + r"\\[Pasted text #\\d+\\]")
        Then run this test.
    RED result: the real '❯\\xa0[Pasted text #1]' placeholder no longer matches ->
    submitted_echo_present stays False -> delivery never confirms -> task_delivery='failed' ->
    `assert sess._task_delivery == 'delivered'` FAILS (and a delivery_failed event is published
    instead of the bracketed-paste + Enter).  Restore with `git checkout daemon/drivers/claude.py`.
    GREEN: delivery confirmed exactly once off the real NBSP frame; one 'stopped' terminal event.
    """
    frames = raw_frames("s-b8a30317-delivery.raw")
    # NON-VACUITY: the fixture must actually carry the real NBSP paste placeholder that confirms
    # delivery — otherwise a 'delivered' assertion would be vacuous.
    assert any("❯\xa0[Pasted text" in f for f in frames), (
        "s-b8a30317-delivery must contain the real '❯\\xa0[Pasted text #1]' placeholder frame")

    # _ensure_ask_mode sleeps between toggle attempts; neutralize so the in-process drive is fast and
    # deterministic (mirrors the synthetic respond/delivery tests patching daemon.session.time.sleep).
    monkeypatch.setattr("daemon.session.time.sleep", lambda *a, **k: None)
    sess, ev, handle = delivery_run(tmp_path, frames, task="create a util logging.py")

    # DELIVERY GLUE (what test_replay_trail bypasses): the real paste echo confirmed delivery exactly
    # ONCE — the task typed ONCE as a single bracketed paste, then Enter pressed ONCE, after it.
    assert sess._task_delivery == "delivered"
    paste_writes = [w for w in handle.writes if "create a util logging.py" in w]
    assert len(paste_writes) == 1                                   # typed once, never re-typed
    assert paste_writes[0] == "\x1b[200~create a util logging.py\x1b[201~"   # ONE bracketed paste
    assert handle.writes.count("\r") == 1                          # Enter pressed exactly once
    assert handle.writes[-1] == "\r"                               # Enter last, OUTSIDE the paste
    # the first user marker was appended on the confirmed delivery (0 == none appended)
    assert sess._dialog.last_user_input_offset() > 0

    # SCOPE NOTE: This oracle deliberately asserts delivery-confirm-once + one terminal, but NOT
    # "blocked published exactly once". Neutralizing _wait_until_ready in the harness (for deterministic
    # in-process replay) creates transient startup-frame artifacts where _emit_blocked fires on frames
    # that production's settle-wait would skip; a full count assertion would be brittle and test-harness-
    # dependent rather than invariant. The core delivery guarantee (confirm once, enter once) is covered.

    # TERMINAL GLUE: _finish publishes exactly ONE terminal event with the correct kind. The capture
    # ends with the child still alive (stopped at the box) -> the handle sets _stop -> 'stopped'.
    terminals = [e.kind for e in ev._events
                 if e.kind in ("stopped", "done", "crashed", "delivery_failed")]
    assert terminals == ["stopped"], f"expected exactly one 'stopped' terminal, got {terminals}"
    assert sess._terminal_kind == "stopped"
    assert sess.snapshot()["control_state"] == "terminal"
