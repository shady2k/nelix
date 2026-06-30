"""Tier-3 session-loop replay oracles (nelix-5gc Task 6) — GLUE that only the real Session owns.

Tiers 1-2 drive renderer + ClaudeDriver + BeliefEngine directly and BYPASS daemon.session.Session.
These oracles replay REAL captures THROUGH the real Session (via tests/_session_replay.py's
RawReplayHandle, the same in-process handle pattern tests/test_session.py uses) to assert glue the
lower tiers cannot reach:

  I6b  — modal respond ROUTING: a real numbered-modal capture drives Session to publish a
          modal_choice decision; Session.respond(valid id) must actuate driver.select_option
          (digit+CR as ONE PTY write) and record the option LABEL; respond(invalid id) is rejected
          and the decision stays pending.  Real-capture version of the synthetic
          test_session.py::test_respond_to_modal_routes_to_select_option.

Each oracle is non-vacuous: it asserts the modal decision / delivery window was ACTUALLY reached
before asserting the routing / once-only behaviour.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._session_replay import (   # noqa: E402
    replay_session, capture_frames,
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
