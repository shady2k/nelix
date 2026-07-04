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
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._session_replay import (   # noqa: E402
    replay_session, delivery_run, delivery_drive, respond_via_monitor, capture_frames, raw_frames,
    replay_hooks, _wire, RawReplayHandle, Spec, _GOLDEN,
)
from daemon.drivers.claude import ClaudeDriver   # noqa: E402
from daemon.observation import ObservationCtx   # noqa: E402

_PERM = Path(__file__).resolve().parent / "golden" / "claude" / "permission_prompt"

# The real task captured in s-639b6474 (from its meta.json). Delivery must confirm off the REAL paste
# echo this task leaves in the capture (frames 19+), so a held dummy string would fail delivery and
# never reach delivery_confirmed — the exact point Fix 1 retires the orphan blocked at.
_PHANTOM_TASK = (
    "Add a GET /health endpoint to the Go service in this repo. The endpoint must return JSON: "
    '{"status":"ok","uptime":N} where N is the server\'s uptime in seconds (integer). Use TDD: '
    "write the test first, run it to see it fail, then implement the endpoint, run the test to see "
    "it pass. Do NOT commit anything — just make the code changes and show the test output. Follow "
    "existing code conventions in the repo.")


# ─────────────────────────────────────────────────────────────────────────────
# pre-delivery modal affordance — a trust/permission interstitial must keep its
# prompt_kind + options so respond() routes the answer via driver.select_option
# ─────────────────────────────────────────────────────────────────────────────
# Regression (live s-9c0b6eeb): a numbered interstitial that appears BEFORE the task is delivered is
# surfaced by Session._emit_blocked. That path published the `blocked` decision with prompt_kind=None
# and options=[] even though ClaudeDriver.observe() classified the frame as a modal_choice with parsed
# options. respond() keys is_modal off prompt_kind, so the modal affordance was lost and the answer was
# typed as FREE TEXT — a bare "1" leaked into Claude's prompt as a stray follow-up message. Drive the
# REAL trust-dialog frame through the pre-delivery tick and assert the decision keeps its affordance.

def test_predelivery_modal_keeps_prompt_kind_and_options(tmp_path):
    trust = (_PERM / "trust-dialog.txt").read_text()
    # NON-VACUITY: the real frame must actually be a numbered modal the driver can classify.
    assert "❯ 1. Yes, I trust this folder" in trust and "2. No, exit" in trust

    sess, ev, clock = _wire(tmp_path, Spec())
    sess._handle = RawReplayHandle([trust], clock=clock)
    sess._handle._dialog = sess._dialog
    sess._handle.pump()                      # advance to the trust frame
    sess._delivery_tick(trust)               # pre-delivery: observe → _emit_blocked

    dec = sess.snapshot().get("decision")
    assert dec is not None and dec["kind"] == "blocked"
    assert dec["task_delivery"] == "pending"                 # still pre-delivery
    assert dec["prompt_kind"] == "modal_choice"              # RED: was None (affordance dropped)
    assert [o["id"] for o in dec["options"]] == ["1", "2"]   # RED: was [] (options dropped)


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
# nelix-32f — AskUserQuestion hook prompt_kind collision (one stable modal decision)
# ─────────────────────────────────────────────────────────────────────────────
# A single on-screen AskUserQuestion modal emits BOTH PreToolUse[AskUserQuestion] (-> modal_choice)
# AND PermissionRequest (-> permission_choice) in this claude build (daemon log s-9610d25c @
# 20:57:20: no concurrent Bash tool — AskUserQuestion itself fires the PermissionRequest).
# daemon/belief.py keyed the pending slot by prompt_kind:epoch, so the two hooks produced two
# DIFFERENT decision keys and the second SUPERSEDED the first; when PermissionRequest arrived second
# the stable modal_choice decision was withdrawn, so respond() rejected every option answer
# (missing_decision_id -> invalid_option x3) and the orchestrator could never answer the modal.
# Compounding factor: a hook-derived modal carries NO options (the hook path's Publish payload omits
# them), so even modulo the collision respond(option_id) is invalid_option. The fix collapses the two
# hooks to ONE stable modal_choice decision (belief.py) AND attaches the on-screen options to it
# (session.py) so the orchestrator can answer end-to-end.

def test_askuserquestion_collision_one_answerable_modal_decision(tmp_path):
    """Drive the REAL session with the staged ground-truth hook sequence (UserPromptSubmit ->
        PreToolUse[AskUserQuestion] -> PermissionRequest) over the real 6-option 'Next step' modal
        capture. The collision MUST collapse to exactly ONE respondable modal_choice decision whose
        decision_id is stable, whose options are the on-screen 6, and which respond(option_id) answers
        end-to-end (no invalid_option). RED on current code: the permission hook supersedes modal_choice
        (prompt_kind becomes permission_choice) and the decision carries NO options -> respond('1') is
        invalid_option. GREEN: one stable modal_choice with options; respond('1') -> select_option."""
    frames = capture_frames("s-9610d25c-askuserquestion-collision.capture")
    modal = frames[-1]
    # NON-VACUITY: the real capture ends on the 6-option AskUserQuestion modal.
    assert "Next step" in modal and "Enter to select" in modal, (
        "fixture must end on the AskUserQuestion 6-option modal")

    sess, ev, clock = _wire(tmp_path, Spec())
    sess._handle = RawReplayHandle([modal], clock=clock)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "delivered"            # the collision is mid-turn (post-delivery)
    hooks = _GOLDEN / "s-9610d25c-askuserquestion-collision.hooks.jsonl"
    trail = replay_hooks(sess, hooks)
    # the trail ends in the modal pause regardless of which hook "won" the collision.
    assert trail[-1][1] == "awaiting_user", trail

    dec = sess.snapshot().get("decision")
    assert dec is not None, "the collision must leave ONE pending decision"
    # AC2: prompt_kind reflects the real modal (modal_choice), NOT permission_choice.
    assert dec["prompt_kind"] == "modal_choice", (
        f"the modal's prompt_kind must survive the permission hook (got {dec.get('prompt_kind')!r})")
    assert dec["kind"] == "waiting_for_user"
    did = dec["decision_id"]
    # AC3/AC5: the on-screen 6 options are attached so respond(option_id) can route to select_option.
    assert [o["id"] for o in dec["options"]] == ["1", "2", "3", "4", "5", "6"], (
        f"a hook-derived modal must carry the screen's options (got {dec.get('options')!r})")
    # AC3: a valid option id is answered end-to-end (select_option -> digit+CR as ONE write).
    out = sess.respond("1", decision_id=did)
    assert out.status == "resumed", f"respond(valid option) must succeed (got {out.status})"
    assert "1\r" in sess._handle.writes, "select_option must type digit+CR as one write"
    assert "1" not in sess._handle.writes, "NOT the free-text two-write path"


def test_askuserquestion_collision_prefers_modal_choice_under_reordered_delivery(tmp_path):
    """AC2 (session-level): hooks are delivered as separate HTTP POSTs, so PermissionRequest may reach
        nelix BEFORE PreToolUse[AskUserQuestion]. The Session's decision must STILL end modal_choice
        with a STABLE decision_id (the late modal hook UPGRADES the published decision in place via the
        re-emit prompt_kind refresh), carry the screen's 6 options, and remain answerable end-to-end.
        RED without session.py's re-emit prompt_kind refresh: the engine re-emits modal_choice but the
        Session keeps the stale permission_choice, so the decision's prompt_kind never upgrades."""
    from daemon.hooks import HookEvent
    frames = capture_frames("s-9610d25c-askuserquestion-collision.capture")
    modal = frames[-1]
    sess, ev, clock = _wire(tmp_path, Spec())
    sess._handle = RawReplayHandle([modal], clock=clock)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "delivered"

    def fire(event, tool_name=None):
        sess.on_hook(HookEvent(session_id=sess._id, event=event, tool_name=tool_name))
        sess._loop_once()

    fire("UserPromptSubmit")
    fire("PermissionRequest")                            # permission arrives FIRST
    dec_perm = sess.snapshot()["decision"]
    assert dec_perm["prompt_kind"] == "permission_choice"
    did = dec_perm["decision_id"]
    fire("PreToolUse", tool_name="AskUserQuestion")      # modal arrives SECOND -> in-place upgrade
    dec = sess.snapshot()["decision"]
    assert dec is not None
    assert dec["decision_id"] == did, "the upgrade is in place: decision_id stays stable"
    assert dec["prompt_kind"] == "modal_choice", (
        f"the late modal hook must upgrade the decision to modal_choice (got {dec.get('prompt_kind')!r})")
    # the modal's 6 options are attached (screen-sourced); the upgraded decision is answerable.
    assert [o["id"] for o in dec["options"]] == ["1", "2", "3", "4", "5", "6"]
    out = sess.respond("1", decision_id=did)
    assert out.status == "resumed", f"respond(valid option) must succeed after the upgrade (got {out.status})"
    assert "1\r" in sess._handle.writes


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

    # daemon.session.time.sleep is used by the delivery confirm-poll loop; neutralize so the
    # in-process drive is fast and deterministic (mirrors the synthetic respond/delivery tests).
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


# ─────────────────────────────────────────────────────────────────────────────
# phantom blocked — a pre-delivery `blocked` must NOT survive past delivery_confirmed
# ─────────────────────────────────────────────────────────────────────────────
# Regression (live s-639b6474, root-caused + Codex-confirmed): a fresh Claude session shows the
# "trust this folder?" modal; the orchestrator answers "1" (delivered, agent works). But a SECOND
# blocked decision is published for the same modal/transition right after the answer, BEFORE
# delivery. Its answer later lands as a stray "1" in the now-working session. Root cause: _emit_blocked
# dedups on the raw normalized-frame fingerprint, so a transitional repaint (different fp, same logical
# prompt) mints a FRESH blocked (new decision_id) that nothing withdraws; the wake layer (pending())
# keeps surfacing it past delivery -> the orchestrator answers -> stray digit.
#
# The invariant to establish: `blocked` exists ONLY pre-delivery. These three oracles lock the three
# fix points over the SAME real capture (s-639b6474):
#   (1) the orphan minted on a post-answer transition frame is WITHDRAWN at delivery_confirmed, and a
#       stale answer to it is REFUSED (nothing typed) — Fixes 1 + 2;
#   (2) a modal that FLICKERS across many raw frames (same options) emits EXACTLY ONE blocked — Fix 3;
#   (3) a `blocked` answered after delivery is REFUSED before the PTY write — Fix 2's race branch.
# NOTE on the trust modal in THIS capture: it renders as a single frame, so it does not itself flicker;
# the phantom here reproduces via the `unknown` transition frames (13/14) that follow the answer — the
# SAME invariant (a blocked surviving past delivery). The modal-flicker "exactly one published" is
# locked separately by (2) on the gopls-lsp modal, which flickers across 7 real frames.

def test_predelivery_phantom_blocked_withdrawn_and_stale_respond_refused(tmp_path, monkeypatch):
    """RED evidence (live phantom-blocked; revert the three fixes to see it):
        the orphan blocked minted on a post-answer transition frame is never withdrawn, so after
        delivery_confirmed ev.pending() still returns it and a second respond() to its id is
        'resumed' — typing '1' then '\\r' into the now-working session (the stray digit).
    GREEN: Fix 1 withdraws the pending blocked at delivery_confirmed (pending() -> None); a stale
        respond() is refused and types NOTHING."""
    monkeypatch.setattr("daemon.session.time.sleep", lambda *a, **k: None)
    frames = raw_frames("s-639b6474-phantom-blocked.raw")
    # NON-VACUITY: the real capture carries the trust modal nelix surfaces pre-delivery.
    assert any("1. Yes, I trust this folder" in f and "2. No, exit" in f for f in frames)

    # Answer the trust modal at exactly the point the orchestrator would (right after it is published),
    # then let the drive continue through the transition frames into delivery. Record the modal's own
    # decision_id so the test can single out the transition-frame ORPHAN below (not just any blocked).
    answered = {"done": False, "modal_id": None}

    def answer_trust(dec, _sess):
        if not answered["done"] and dec.get("kind") == "blocked" \
                and dec.get("prompt_kind") == "modal_choice":
            answered["done"] = True
            answered["modal_id"] = dec["decision_id"]
            return "1"                                  # accept the trust folder
        return None

    sess, ev, handle = delivery_drive(tmp_path, frames, task=_PHANTOM_TASK, respond=answer_trust)

    # NON-VACUITY: the trust modal blocked was reached, answered (select_option -> '1\r'), and the
    # real paste echo confirmed delivery — otherwise the assertions below would be vacuous.
    assert answered["done"], "the trust modal blocked must be reached and answered"
    assert sess._task_delivery == "delivered", "delivery must confirm off the real paste echo"
    assert any("1\r" in w for w in handle.writes), "the trust answer is typed once via select_option"

    blocked_ids = []
    for e in ev._events:
        if e.kind == "blocked" and e.decision_id and e.decision_id not in blocked_ids:
            blocked_ids.append(e.decision_id)

    # The REPRODUCTION MECHANISM (not a count): a transition-frame ORPHAN — a blocked minted on a
    # post-answer `unknown`/transition frame, DISTINCT from the answered modal — must be exercised by
    # the capture. This is the phantom class; singling it out means a driver re-classification that
    # stops minting it can't silently turn this oracle into a 0==0 pass.
    orphan_ids = [i for i in blocked_ids if i != answered["modal_id"]]
    assert orphan_ids, (
        "the capture must mint a transition-frame orphan blocked distinct from the answered modal; "
        "if not, this oracle no longer exercises the phantom mechanism and must be revisited")

    # (a) INVARIANT — `blocked` exists ONLY pre-delivery: every orphan is RESOLVED (answered or
    # superseded) and none is left pending for the wake layer. RED on current code: the orphan minted
    # on the last transition frame survives as unresolved/pending (ev.pending() returns it). GREEN:
    # Fix 1 withdraws it at delivery_confirmed (resolved 'superseded').
    assert ev.pending("s1") is None, "no blocked may remain pending once delivery is confirmed"
    for oid in orphan_ids:
        reasons = [e.resolved_reason for e in ev._events
                   if e.kind == "blocked" and e.decision_id == oid]
        assert reasons and all(r is not None for r in reasons), (
            f"orphan blocked {oid} must be retired (resolved), not left pending for the wake layer")

    # (b) a stale answer to an orphan id is REFUSED — NOTHING is typed (no stray digit). RED on
    # current code: respond() to the orphan -> 'resumed' + ['1', '\r'] written into the delivered
    # session. GREEN: refused (no_pending/stale) and the PTY is untouched past the trust answer.
    stale_id = orphan_ids[-1]                           # the last-minted orphan (the phantom class)
    writes_before = len(handle.writes)
    out = sess.respond("1", decision_id=stale_id)
    assert out.status != "resumed", "a stale blocked answer must be refused, not resumed"
    assert handle.writes[writes_before:] == [], "nothing may be typed into the delivered session"


def test_emit_blocked_collapses_a_flickering_modal_to_one_blocked(tmp_path, monkeypatch):
    """Targeted _emit_blocked semantic-dedup unit test (F4 — scope stated honestly). This is NOT a full
        pre-delivery lifecycle reproduction: no real capture in s-639b6474 carries a pre-delivery modal
        that flickers (the trust modal renders as a single frame). Instead it takes the REAL flickering
        gopls-lsp modal frames from the capture (there they appear POST-delivery, while the agent
        streams an edit above the modal — the repaint's volatility is that streaming scrollback, not the
        cursor) and drives them through the PRE-delivery _emit_blocked path SYNTHETICALLY
        (delivery_drive primes task_delivery='pending') to unit-test the dedup in isolation.
        RED evidence (revert Fix 3): raw-frame dedup mints a FRESH blocked per repaint -> 7 distinct
        decision_ids for one modal. GREEN: the semantic key (prompt_kind + option ids/labels) collapses
        the flicker to exactly ONE blocked."""
    monkeypatch.setattr("daemon.session.time.sleep", lambda *a, **k: None)
    frames = raw_frames("s-639b6474-phantom-blocked.raw")
    # Extract the real gopls-lsp install modal FLICKER: consecutive modal_choice frames carrying the
    # SAME 4 options but DISTINCT raw normalized frames (the repaint nelix used to mint one-per).
    drv = ClaudeDriver()
    ctx = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)
    gopls = [f for f in frames
             if "gopls-lsp" in f and drv.observe(f, ctx).prompt_kind == "modal_choice"]
    # NON-VACUITY: the fixture must carry a genuine flicker (>=2 distinct raw frames, one logical modal).
    assert len(gopls) >= 2, "fixture must carry the gopls modal flicker (>=2 repaint frames)"
    assert len({drv.normalize_frame(f) for f in gopls}) >= 2, (
        "the flicker frames must differ in their RAW normalized frame (else raw-fp dedup already holds)")

    sess, ev, _handle = delivery_drive(tmp_path, gopls, task="x", max_iters=len(gopls) + 3)

    ids = []
    for e in ev._events:
        if e.kind == "blocked" and e.decision_id and e.decision_id not in ids:
            ids.append(e.decision_id)
    assert len(ids) == 1, (
        f"a flickering modal must emit EXACTLY ONE blocked (got {len(ids)}: {ids})")


def test_predelivery_two_same_label_modals_each_publish(tmp_path, monkeypatch):
    """F2 (dedup key too coarse). RED evidence (revert the _blocked_dedup reset): the semantic key
        (prompt_kind + option ids/labels) PERSISTS after the prior blocked is answered, so a SECOND
        pre-delivery modal that shares option labels (two generic '1. Yes / 2. No' prompts asking
        different questions) is silently dropped -> the orchestrator never sees it (stuck). GREEN: the
        dedup key is dropped once no blocked is pending, so each distinct modal publishes.
        (Same option labels for both is the point: it proves the dedup can't collapse two DIFFERENT
        modals that merely share a Yes/No label set.)"""
    monkeypatch.setattr("daemon.session.time.sleep", lambda *a, **k: None)

    def choice_modal(question):
        # A realistic numbered modal: a top rule border, the question/title, then a selected option
        # (cursor on it) + >=2 options. Same option labels for both modals, DIFFERENT question text.
        # (Border ABOVE the question — real claude modals render their rule border above the title —
        # so the driver's modal_body_fp captures rule+question, not just the border.)
        return "\n".join([" " + "─" * 75, " " + question,
                          "❯ 1. Yes", "  2. No", "  shift+tab to cycle"])

    modal_a = choice_modal("Enable anonymous usage analytics for this workspace?")
    modal_b = choice_modal("Delete the cached build artifacts to free disk space?")
    drv = ClaudeDriver()
    ctx = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)
    oa, ob = drv.observe(modal_a, ctx), drv.observe(modal_b, ctx)
    # NON-VACUITY: both classify as modal_choice with IDENTICAL option labels (the collision class).
    assert oa.prompt_kind == ob.prompt_kind == "modal_choice"
    assert ([(o.id, o.label) for o in oa.options]
            == [(o.id, o.label) for o in ob.options] == [("1", "Yes"), ("2", "No")])

    sess, ev, _clock = _wire(tmp_path, Spec())
    sess._handle = RawReplayHandle([modal_a], clock=_clock)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "pending"
    sess._handle.pump()
    sess._delivery_tick(modal_a)                                  # publish modal A
    dec_a = sess.snapshot().get("decision")
    assert dec_a is not None and dec_a["prompt_kind"] == "modal_choice"
    id_a = dec_a["decision_id"]
    # C1: answer A via the MONITOR (sole writer) — respond() enqueues + blocks, this thread drains on
    # modal_a (still on screen -> submit). A pre-delivery blocked answer is never written by respond().
    assert respond_via_monitor(sess, "1", id_a, modal_a).status == "resumed"

    # A SECOND pre-delivery modal with the SAME option labels but a DIFFERENT question appears.
    sess._handle = RawReplayHandle([modal_b], clock=_clock)
    sess._handle._dialog = sess._dialog
    sess._handle.pump()
    sess._delivery_tick(modal_b)                                  # must publish B (not be deduped)
    dec_b = sess.snapshot().get("decision")
    assert dec_b is not None, "the second same-label modal must publish (was silently dropped pre-fix)"
    assert dec_b["decision_id"] != id_a                           # a fresh decision, not a re-emit
    assert dec_b["prompt_kind"] == "modal_choice"


def test_respond_refuses_blocked_after_delivery(tmp_path, monkeypatch):
    """Fix 2 (race closer). RED evidence (revert Fix 2): a pre-delivery `blocked` whose modal is
        already gone (delivery confirmed between publish and answer) is still claimed and ANSWERED —
        respond() types select_option('1') -> '1\\r' into the now-working session (the stray digit).
        GREEN: refused ('stale') BEFORE the PTY write, NOTHING typed."""
    monkeypatch.setattr("daemon.session.time.sleep", lambda *a, **k: None)
    trust = (_PERM / "trust-dialog.txt").read_text()
    sess, ev, _clock = _wire(tmp_path, Spec())
    sess._handle = RawReplayHandle([trust], clock=_clock)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "pending"
    sess._handle.pump()                                 # advance to the trust frame
    sess._delivery_tick(trust)                          # pre-delivery: observe -> _emit_blocked

    dec = sess.snapshot().get("decision")
    assert dec is not None and dec["kind"] == "blocked"   # NON-VACUITY: a blocked is pending
    assert dec["prompt_kind"] == "modal_choice"
    did = dec["decision_id"]

    # Delivery confirms AFTER the blocked was published but BEFORE the answer lands (the race): the
    # modal is gone, the agent is now working. Answering must NOT type into the live session.
    sess._task_delivery = "delivered"
    writes_before = len(sess._handle.writes)
    out = sess.respond("1", decision_id=did)
    assert out.status != "resumed", "a blocked answered after delivery must be refused, not resumed"
    assert sess._handle.writes[writes_before:] == [], "nothing may be typed into the delivered session"


def test_predelivery_blocked_answer_never_types_when_delivery_wins_the_TOCTOU(tmp_path, monkeypatch):
    """C1 (F1 closed for real via single-writer PTY). Codex's exact interleaving: respond() commits to a
        pre-delivery blocked answer (ENQUEUES it, delivery still pending), then the MONITOR confirms
        delivery (pending->delivered) in the window AFTER the enqueue and BEFORE it drains the answer.
        The monitor is the SOLE writer of a pre-delivery answer, so when it re-observes the screen at
        drain time and finds delivery has won it ABORTS — NOTHING is typed and respond() returns a
        non-answered outcome. The decision is resolved 'superseded' (NOT 'answered') — M1: an aborted
        answer is never recorded as answered.

        Deterministic model of the enqueue->drain window: respond() runs on the RPC thread
        (respond_via_monitor) and ENQUEUES the answer; THIS (monitor) thread then confirms delivery
        (flips _task_delivery to 'delivered') in the helper's before_drain seam — between the enqueue
        and the drain, the EXACT window the single-writer design protects — before draining.

        RED evidence (drop the `self._task_delivery == "pending"` re-check from _drain_pending_answer's
        submit condition): delivery won in the enqueue->drain window but the drain no longer notices ->
        the on-screen modal still matches the answer's target -> the answer is submitted -> '1\\r' is
        typed into the delivered session (the stray digit) and status is 'resumed'. GREEN: the drain
        re-checks _task_delivery at drain time, sees delivery won, and aborts (nothing typed)."""
    monkeypatch.setattr("daemon.session.time.sleep", lambda *a, **k: None)
    trust = (_PERM / "trust-dialog.txt").read_text()
    sess, ev, _clock = _wire(tmp_path, Spec())
    sess._handle = RawReplayHandle([trust], clock=_clock)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "pending"
    sess._handle.pump()                                 # advance to the trust frame
    sess._delivery_tick(trust)                          # pre-delivery: observe -> _emit_blocked
    dec = sess.snapshot().get("decision")
    assert dec is not None and dec["kind"] == "blocked"   # NON-VACUITY: a blocked is pending
    did = dec["decision_id"]

    # Model the monitor confirming delivery in the enqueue->drain window: AFTER respond() has enqueued
    # the answer (delivery still "pending" at enqueue, so the _respond_blocked pre-check does NOT refuse
    # it — only the drain's re-check can catch this) but BEFORE the monitor re-observes the screen.
    def deliver_after_enqueue(s):
        s._task_delivery = "delivered"

    writes_before = len(sess._handle.writes)
    out = respond_via_monitor(sess, "1", did, trust, before_drain=deliver_after_enqueue)
    # C1: NOTHING is typed — the drain aborted because delivery won (no stray digit).
    assert out.status != "resumed", "a pre-delivery answer must not be written once delivery wins"
    assert sess._handle.writes[writes_before:] == [], (
        "no stray digit may be typed into the delivered session")
    # M1: the aborted answer is resolved 'superseded' (honest), NEVER 'answered'.
    reasons = [e.resolved_reason for e in ev._events if e.decision_id == did and e.kind == "blocked"]
    assert reasons, "the blocked decision must be resolved"
    assert all(r == "superseded" for r in reasons), (
        f"an aborted blocked answer must resolve 'superseded', not 'answered' (got {reasons})")


def test_predelivery_multiline_question_modals_distinguished(tmp_path, monkeypatch):
    """I2 (modal_body_fp fingerprints the FULL question block). RED evidence (revert modal_body_fp to the
        first-nonblank-row-above-options heuristic): two modals with IDENTICAL options and an IDENTICAL
        last question row but a DIFFERENT earlier question row collapse to ONE blocked (the heuristic
        keyed only the single row immediately above the options) -> the second is silently dropped. GREEN:
        modal_body_fp fingerprints the contiguous question block, so a difference in an EARLIER row still
        distinguishes the two -> each publishes (and the gopls 7-frame flicker still collapses to one).

        HONEST SCOPE: this is a semantic-dedup-key regression for modal_body_fp's full-block
        fingerprint, NOT a direct red/green vs main. Main deduped `blocked` on the RAW normalized frame,
        under which two textually-distinct modals always differ (both publish) — so this oracle PASSES
        on main too. The red is specifically vs the modal_body_fp single-row heuristic (the
        intermediate design that keyed only the last question row); it locks that refinement, not the
        raw-frame -> semantic-key switch itself (the gopls flicker test locks the latter)."""
    monkeypatch.setattr("daemon.session.time.sleep", lambda *a, **k: None)

    def choice_modal(q1, q2):              # a DENSE two-line question (no blank between the rows)
        return "\n".join([" " + "─" * 75, " " + q1, " " + q2,
                          "❯ 1. Yes", "  2. No", "  shift+tab to cycle"])
    modal_a = choice_modal("Install the gopls LSP plugin now?", "This enables go-to-definition.")
    modal_b = choice_modal("Install the rust-analyzer LSP plugin now?", "This enables go-to-definition.")
    drv = ClaudeDriver()
    ctx = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)
    oa, ob = drv.observe(modal_a, ctx), drv.observe(modal_b, ctx)
    # NON-VACUITY: identical options AND identical LAST question row (the row the old heuristic keyed).
    assert [(o.id, o.label) for o in oa.options] == [(o.id, o.label) for o in ob.options]
    body_a = drv.modal_body_fp(drv.normalize_frame(modal_a))
    body_b = drv.modal_body_fp(drv.normalize_frame(modal_b))
    assert body_a is not None and body_a != body_b, (
        "the full question block must distinguish two modals that share only their last question row")

    # Drive both through the real Session pre-delivery path: each must publish its OWN blocked.
    sess, ev, _clock = _wire(tmp_path, Spec())
    sess._handle = RawReplayHandle([modal_a], clock=_clock)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "pending"
    sess._handle.pump()
    sess._delivery_tick(modal_a)                                  # publish modal A
    id_a = sess.snapshot()["decision"]["decision_id"]
    assert respond_via_monitor(sess, "1", id_a, modal_a).status == "resumed"   # answer A via the monitor

    sess._handle = RawReplayHandle([modal_b], clock=_clock)
    sess._handle._dialog = sess._dialog
    sess._handle.pump()
    sess._delivery_tick(modal_b)                                  # must publish B (distinct body)
    dec_b = sess.snapshot().get("decision")
    assert dec_b is not None, "the second multi-line modal must publish (was silently dropped pre-I2)"
    assert dec_b["decision_id"] != id_a


# ─────────────────────────────────────────────────────────────────────────────
# C2 — a blocked answer must be typed ONLY into the SAME modal it targets
# ─────────────────────────────────────────────────────────────────────────────
# Codex 3rd-pass Critical: _drain_pending_answer runs BEFORE _delivery_tick each tick, so when the
# answer for modal A is enqueued but the screen now shows a DIFFERENT modal B (A superseded before
# B's _emit_blocked republished -> self._decision is STILL A), A's keystrokes would be submitted into
# B — answering the WRONG trust/permission prompt. The drain must verify the on-screen modal is the
# SAME logical prompt the answer targets (the F2 semantic dedup key), not merely "a modal".

def test_predelivery_blocked_answer_not_typed_into_a_different_modal(tmp_path, monkeypatch):
    """C2 (CRITICAL). RED on 6305c66: _drain_pending_answer submits whenever the current frame is any
        modal_choice and self._decision is still the answered one (B not yet republished) -> A's '1\\r'
        is typed into modal B (the wrong prompt answered). GREEN: the drain compares the current modal's
        semantic identity (prompt_kind + option ids/labels + modal_body_fp) to the answer's TARGET
        identity (captured at publish, carried on the decision, frozen into the pending answer at
        enqueue); a mismatch ABORTS — nothing typed, respond returns a non-answered outcome.

    Deterministic model of the window: respond() enqueues the answer for A; THIS (monitor) thread then
    drains against modal B's frame (A superseded by B before B's _emit_blocked re-published, so
    self._decision is still A — exactly the frame the old prompt_kind-only check could not distinguish)."""
    monkeypatch.setattr("daemon.session.time.sleep", lambda *a, **k: None)

    def choice_modal(question):
        # A realistic numbered modal: rule border above the question, the question, a selected option
        # (cursor on it) + >=2 options. Same option labels, DIFFERENT question text (different body).
        return "\n".join([" " + "─" * 75, " " + question,
                          "❯ 1. Yes", "  2. No", "  shift+tab to cycle"])
    modal_a = choice_modal("Enable anonymous usage analytics for this workspace?")
    modal_b = choice_modal("Delete the cached build artifacts to free disk space?")
    drv = ClaudeDriver()
    ctx = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)
    # NON-VACUITY: A and B are both numbered modals but DISTINCT logical prompts (different body).
    oa, ob = drv.observe(modal_a, ctx), drv.observe(modal_b, ctx)
    assert oa.prompt_kind == ob.prompt_kind == "modal_choice"
    body_a = drv.modal_body_fp(drv.normalize_frame(modal_a))
    body_b = drv.modal_body_fp(drv.normalize_frame(modal_b))
    assert body_a is not None and body_a != body_b, (
        "the two modals must be distinct logical prompts (different question body)")

    sess, ev, _clock = _wire(tmp_path, Spec())
    sess._handle = RawReplayHandle([modal_a], clock=_clock)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "pending"
    sess._handle.pump()
    sess._delivery_tick(modal_a)                                  # publish modal A (carries its identity)
    dec_a = sess.snapshot().get("decision")
    assert dec_a is not None and dec_a["prompt_kind"] == "modal_choice"
    id_a = dec_a["decision_id"]

    # respond() enqueues the answer for A; the MONITOR then drains against modal B's frame (the screen
    # moved to a different prompt while self._decision is still A).
    writes_before = len(sess._handle.writes)
    out = respond_via_monitor(sess, "1", id_a, modal_b)
    # C2: A's keystrokes are NOT typed into B; respond returns a non-answered outcome.
    assert out.status != "resumed", "an answer for modal A must not be submitted into modal B"
    assert sess._handle.writes[writes_before:] == [], (
        "no keystrokes may be typed when the on-screen modal differs from the answer's target")
    # M1: the aborted answer resolves 'superseded' (honest), NEVER 'answered'.
    reasons = [e.resolved_reason for e in ev._events if e.decision_id == id_a and e.kind == "blocked"]
    assert reasons and all(r == "superseded" for r in reasons), (
        f"a mismatched-modal abort must resolve 'superseded', not 'answered' (got {reasons})")


# ─────────────────────────────────────────────────────────────────────────────
# I3 — a waiting respond() is released on EVERY teardown/fail/stop exit, not just delivery/drain
# ─────────────────────────────────────────────────────────────────────────────
# Codex 3rd-pass Important: a respond() blocked on a pre-delivery answer is woken on normal
# delivery/drain, but on a teardown/stop/fail exit (which all funnel through _finish) the pending
# answer was never aborted -> the RPC thread waits the FULL respond_write_seconds. The fix centralizes
# the abort in _finish (the single monitor-thread exit), releasing the waiter promptly.

class _ShortRespondSpec(Spec):
    # Shrink the write window so the RED (full-window wait) is fast, not a real 5s stall.
    respond_write_seconds = 1.0


def test_pending_answer_released_promptly_when_session_goes_terminal(tmp_path, monkeypatch):
    """I3. RED on 6305c66: _finish does not abort _pending_answer, so a respond() blocked on the
        answer waits the FULL respond_write_seconds before its own timeout reclaim -> the elapsed
        assertion (well under the window) FAILS. GREEN: _finish aborts the pending answer (nothing
        typed, outcome 'terminal') on every monitor-thread exit, so respond() wakes promptly and
        returns a non-answered outcome."""
    monkeypatch.setattr("daemon.session.time.sleep", lambda *a, **k: None)
    trust = (_PERM / "trust-dialog.txt").read_text()
    sess, ev, _clock = _wire(tmp_path, _ShortRespondSpec())
    sess._handle = RawReplayHandle([trust], clock=_clock)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "pending"
    sess._handle.pump()
    sess._delivery_tick(trust)                               # publish the trust blocked
    dec = sess.snapshot().get("decision")
    assert dec is not None and dec["kind"] == "blocked"      # NON-VACUITY: a blocked is pending
    did = dec["decision_id"]

    # respond() runs on a worker (the RPC thread): it ENQUEUES the answer and blocks on the monitor.
    box = {}

    def rpc():
        try:
            box["out"] = sess.respond("1", decision_id=did)
        except BaseException as exc:                        # pragma: no cover - surfaced via assert
            box["err"] = exc

    t = threading.Thread(target=rpc, daemon=True)
    t.start()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        with sess._lock:
            enqueued = sess._pending_answer is not None
        if enqueued:
            break
        time.sleep(0)
    else:
        t.join(timeout=1.0)
        raise AssertionError(box.get("err") or "respond() never enqueued a pending answer")

    # The session goes terminal (operator stop / delivery fail) WHILE respond() is waiting. Every
    # monitor-thread exit funnels through _finish, which aborts the still-pending answer -> the waiter
    # is released promptly (nothing typed).
    sess._stop.set()
    start = time.monotonic()
    sess._finish()
    t.join(timeout=10.0)
    elapsed = time.monotonic() - start
    if "err" in box:
        raise box["err"]
    out = box["out"]
    assert out.status != "resumed", "a terminal session must not resume a pending answer"
    assert elapsed < sess._spec.respond_write_seconds / 2, (
        f"respond() must wake well under respond_write_seconds on teardown (took {elapsed:.2f}s)")
    assert sess._handle.writes == [], "nothing may be typed when the session tears down"


# ─────────────────────────────────────────────────────────────────────────────
# E1 — close the enqueue-after-abort teardown race (I3 residual)
# ─────────────────────────────────────────────────────────────────────────────
# Codex 4th-pass Important: _finish ran _abort_pending_answer BEFORE it raised the _closing gate (which
# was set last, in _finish_cleanup). A respond() racing into that window PASSED its _closing guard and
# enqueued a _pending_answer AFTER the abort ran — and since the monitor is tearing down no tick ever
# drains it, so respond() stalled the FULL respond_write_seconds. Fix: raise the terminal gate in
# _finish BEFORE the abort, so respond()'s enqueue-rejection and the teardown abort share ONE
# lock-guarded gate; every enqueued answer is guaranteed to be drained OR aborted.

def test_respond_in_abort_window_refused_by_finish_gate(tmp_path, monkeypatch):
    """E1 (I3 residual — enqueue-after-abort teardown race). RED on 6cec3f3: _finish aborts BEFORE
       raising _closing, so a respond() that races into the post-abort / pre-closing window enqueues a
       _pending_answer no monitor path drains -> respond() returns 'write_timeout' only after the FULL
       respond_write_seconds. GREEN: _finish raises the terminal gate BEFORE the abort; the respond() in
       the window sees the gate and returns 'terminal' PROMPTLY, nothing typed.

       Deterministic model of the window: patch _abort_pending_answer to run the racing respond()
       immediately AFTER the real abort — i.e. at exactly the post-abort / pre-closing point of _finish."""
    monkeypatch.setattr("daemon.session.time.sleep", lambda *a, **k: None)
    trust = (_PERM / "trust-dialog.txt").read_text()
    sess, ev, _clock = _wire(tmp_path, _ShortRespondSpec())
    sess._handle = RawReplayHandle([trust], clock=_clock)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "pending"
    sess._handle.pump()
    sess._delivery_tick(trust)                               # publish the trust blocked
    dec = sess.snapshot().get("decision")
    assert dec is not None and dec["kind"] == "blocked"      # NON-VACUITY: a blocked is pending
    did = dec["decision_id"]

    # Inject the racing respond() at exactly the post-abort / pre-closing point of _finish.
    raced = {}
    real_abort = sess._abort_pending_answer

    def abort_then_race(reason):
        real_abort(reason)                       # _finish's abort ran (no answer pending -> no-op)
        t0 = time.monotonic()
        raced["out"] = sess.respond("1", decision_id=did)   # RPC thread races into the window
        raced["writes"] = list(sess._handle.writes)
        raced["elapsed"] = time.monotonic() - t0

    sess._abort_pending_answer = abort_then_race

    sess._stop.set()
    sess._finish()                               # abort(race injected) -> publish -> cleanup
    out = raced["out"]
    # GREEN: the window-enqueue is refused by the gate -> 'terminal', promptly, nothing typed.
    # RED on 6cec3f3: out.status == 'write_timeout' after the FULL window (elapsed >= respond_write_seconds).
    assert out.status == "terminal", (
        f"a respond() in the abort->closing window must be refused (got {out.status})")
    assert raced["elapsed"] < sess._spec.respond_write_seconds / 2, (
        f"the window-enqueue must be refused PROMPTLY, not stall the window "
        f"(took {raced['elapsed']:.2f}s)")
    assert raced["writes"] == [], "nothing may be typed into a tearing-down session"


# ─────────────────────────────────────────────────────────────────────────────
# E2 — a None modal_body_fp is uncertainty -> abort (do not submit into a bodyless modal)
# ─────────────────────────────────────────────────────────────────────────────
# Codex 4th-pass Important: _modal_dedup builds the semantic key with a possibly-None body fingerprint,
# and _drain_pending_answer could then SUBMIT on a (prompt_kind, options, None) match — so two distinct
# bodyless modals with identical option labels collapse onto one key and the answer is typed into the
# wrong one. Fix: the drain's submit predicate requires a NON-None body fingerprint on BOTH the frozen
# target and the freshly-observed frame; a None body on either side aborts (nothing typed).

def test_predelivery_bodyless_modal_target_aborts_not_submitted(tmp_path, monkeypatch):
    """E2 (None modal_body_fp -> uncertainty -> abort). RED on 6cec3f3: the bodyless target's
       (prompt_kind, options, None) key matches itself -> the answer is submitted ('resumed'). GREEN:
       the None body is treated as uncertainty -> ABORT (nothing typed, non-answered outcome).

       NO-REGRESSION: every REAL captured modal (s-639b6474 trust + the gopls 7-frame flicker,
       trust-dialog.txt, the run-command/edit permission menus) has a NON-None body fp (verified with
       the real driver against every fixture), so this guard never strands a legitimate answer — a
       bodyless modal is a synthetic identity hole, not a real Claude Code prompt shape."""
    monkeypatch.setattr("daemon.session.time.sleep", lambda *a, **k: None)

    # A numbered modal whose option block is the FIRST content row (no question/title above it) ->
    # ClaudeDriver.modal_body_fp returns None (first_opt == 0). Still a real modal_choice with options.
    modal = "❯ 1. Yes\n  2. No\nEnter to confirm\n"
    drv = ClaudeDriver()
    ctx = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)
    obs = drv.observe(modal, ctx)
    # NON-VACUITY: a numbered modal with options, but BODYLESS (the identity hole this test targets).
    assert obs.prompt_kind == "modal_choice" and len(obs.options) >= 2
    assert drv.modal_body_fp(drv.normalize_frame(modal)) is None, (
        "fixture must be a BODYLESS modal (None body fp) for this test")

    sess, ev, _clock = _wire(tmp_path, Spec())
    sess._handle = RawReplayHandle([modal], clock=_clock)
    sess._handle._dialog = sess._dialog
    sess._task_delivery = "pending"
    sess._handle.pump()
    sess._delivery_tick(modal)                              # publish the bodyless blocked
    dec = sess.snapshot().get("decision")
    assert dec is not None and dec["prompt_kind"] == "modal_choice"
    did = dec["decision_id"]

    # respond() enqueues the answer; the MONITOR drains against the SAME bodyless frame. Under the old
    # predicate the (kind, options, None) key matched itself -> submit. The guard must treat the None
    # body as uncertainty and ABORT.
    writes_before = len(sess._handle.writes)
    out = respond_via_monitor(sess, "1", did, modal)
    assert out.status != "resumed", (
        "a bodyless-modal target is identity-ambiguous -> must NOT be submitted")
    assert sess._handle.writes[writes_before:] == [], (
        "no keystrokes may be typed when the target modal has no body fingerprint")
    # M1: the aborted answer resolves 'superseded' (honest), NEVER 'answered'.
    reasons = [e.resolved_reason for e in ev._events if e.decision_id == did and e.kind == "blocked"]
    assert reasons and all(r == "superseded" for r in reasons), (
        f"a bodyless-target abort must resolve 'superseded', not 'answered' (got {reasons})")
