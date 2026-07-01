"""BeliefEngine.on_hook — the authoritative hook-driven state path (plan Task 6).

Mirrors test_belief_engine.py's style. These exercise the additive `on_hook` entry only (the
screen `tick` path is untouched here; precedence/reconciliation is Task 7). A hook is ground
truth: the first hook flips `hook_mode` to "active", `UserPromptSubmit` opens a fresh turn epoch,
and `Stop` publishes the new non-respondable `idle` decision that keeps the session alive.
"""
from daemon.belief import BeliefEngine, Publish, Withdraw, Finalize
from daemon.hooks import HookObservation
from daemon.clock import FakeClock
from daemon.config import BeliefConfig
from daemon.observation import Observation, ObservationCtx, Heartbeat

CTX = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)


def H(kind, **kw):
    return HookObservation(kind=kind, closes_turn=kw.pop("closes", False),
                           opens_turn=kw.pop("opens", False), clears_pending=kw.pop("clears", False), **kw)


def eng():
    return BeliefEngine(BeliefConfig(), FakeClock(0.0))


def test_hook_stop_publishes_idle_nonrespondable():
    e = eng()
    acts = e.on_hook(H("idle", closes=True), 0.0)
    p = [a for a in acts if isinstance(a, Publish)][0]
    assert p.kind == "idle" and p.respondable is False
    assert e.state.control_state == "idle" and e.hook_mode == "active"


def test_hook_askquestion_publishes_waiting():
    e = eng()
    p = [a for a in e.on_hook(H("waiting_for_user", prompt_kind="modal_choice"), 0.0) if isinstance(a, Publish)][0]
    assert p.kind == "waiting_for_user" and p.respondable


def test_first_hook_withdraws_stale_screen_decision():
    # CRITICAL 1: the screen fallback published a respondable waiting_for_user; then the FIRST hook
    # (unknown->active) takes over. It MUST Withdraw the stale screen decision so a lingering
    # waiting_for_user does not survive into the hook-owned snapshot, and go busy.
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    modal = Observation(prompt_kind="modal_choice", semantic_fp="ask", prompt_fp="box",
                        affordances=frozenset({"accepts_text_input"}), options=("1", "2"))
    acts = e.tick(modal, CTX)                                 # modal publishes immediately (screen path)
    assert any(isinstance(a, Publish) and a.kind == "waiting_for_user" for a in acts)
    assert e.hook_mode == "unknown"
    acts = e.on_hook(H("working", opens=True), 0.0)          # first hook: UserPromptSubmit
    assert any(isinstance(a, Withdraw) for a in acts)         # the stale screen decision is withdrawn
    assert e.state.control_state == "busy" and e.hook_mode == "active"


def test_late_posttooluse_after_stop_is_ignored():
    e = eng()
    e.on_hook(H("idle", closes=True), 0.0)
    assert e.on_hook(H("working"), 0.1) == []            # no new turn -> ignored, stays idle
    assert e.state.control_state == "idle"


def test_late_waiting_for_user_after_stop_is_ignored():
    # CRITICAL 2: after Stop closes the turn (idle), a late/out-of-order waiting_for_user hook
    # (PreToolUse[AskUserQuestion]/PermissionRequest straggler) must NOT resurrect the closed turn —
    # it is ignored (stays idle, no actions), UNLESS a newer UserPromptSubmit reopened the turn.
    e = eng()
    e.on_hook(H("working", opens=True), 0.0)
    e.on_hook(H("idle", closes=True), 0.1)
    assert e.state.control_state == "idle"
    acts = e.on_hook(H("waiting_for_user", prompt_kind="modal_choice"), 0.2)
    assert acts == []                                     # late ask after close -> ignored
    assert e.state.control_state == "idle"                # stays idle, not awaiting_user


def test_late_clears_pending_after_stop_is_ignored():
    # CRITICAL 2: after Stop closes the turn, a late clears_pending hook (PostToolUse[AskUserQuestion]
    # straggler) must NOT drag the session back to busy — it is ignored (stays idle, no actions).
    e = eng()
    e.on_hook(H("working", opens=True), 0.0)
    e.on_hook(H("idle", closes=True), 0.1)
    assert e.state.control_state == "idle"
    acts = e.on_hook(H("working", clears=True), 0.2)
    assert acts == []                                     # late clear after close -> ignored
    assert e.state.control_state == "idle"                # stays idle, not busy


def test_ask_reopened_by_userpromptsubmit_still_publishes():
    # CRITICAL 2 (complement): once a NEWER UserPromptSubmit reopens the turn, a waiting_for_user is
    # NOT a straggler — it must publish again (the closed-turn guard only silences a genuinely closed
    # turn, never a freshly reopened one).
    e = eng()
    e.on_hook(H("working", opens=True), 0.0)
    e.on_hook(H("idle", closes=True), 0.1)
    e.on_hook(H("working", opens=True), 0.2)             # reopen the turn
    acts = e.on_hook(H("waiting_for_user", prompt_kind="modal_choice"), 0.3)
    assert any(isinstance(a, Publish) and a.kind == "waiting_for_user" for a in acts)
    assert e.state.control_state == "awaiting_user"


def test_userpromptsubmit_opens_new_turn_from_idle():
    e = eng()
    e.on_hook(H("idle", closes=True), 0.0)
    ep = e.turn_epoch
    e.on_hook(H("working", opens=True), 0.2)
    assert e.state.control_state == "busy" and e.turn_epoch == ep + 1


def test_posttooluse_askquestion_withdraws_pending():
    e = eng()
    e.on_hook(H("waiting_for_user", prompt_kind="modal_choice"), 0.0)
    acts = e.on_hook(H("working", clears=True), 0.1)
    assert any(isinstance(a, Withdraw) for a in acts) and e.state.control_state == "busy"


def test_duplicate_stop_idempotent():
    e = eng()
    a1 = e.on_hook(H("idle", closes=True), 0.0)
    a2 = e.on_hook(H("idle", closes=True), 0.1)
    assert a2 == []                                       # already idle at same epoch


def test_interrupted_flag_on_idle_payload():
    e = eng()
    p = [a for a in e.on_hook(H("idle", closes=True, interrupted=True), 0.0) if isinstance(a, Publish)][0]
    assert p.payload.get("interrupted") is True


# ---- Task 7: precedence & lost-hook reconciliation in tick() (process > hook > screen) ----
#
# When hook_mode == "active", the screen `tick` path must NOT publish a screen-derived
# waiting_for_user/idle — hooks are ground truth. tick() then only (a) passes the terminal branch,
# (b) runs the watchdog, and (c) applies the bounded lost-hook reconciliation (lost-Stop timeout ->
# intervention_required; a stable free-text screen after a lost Stop -> reconciled idle). During the
# startup grace (hook_mode "unknown", hooks expected) the screen must stay conservative and NOT
# declare a free-text idle; once the grace expires with no hook the session is "unavailable" and the
# screen path behaves EXACTLY as today.

def idle_obs(sfp="idle", pfp="box"):
    # a stable free-text idle screen (the screen-scraper's "turn complete" hypothesis).
    return Observation(prompt_kind="free_text", semantic_fp=sfp, prompt_fp=pfp,
                       affordances=frozenset({"accepts_text_input"}))


def busy_obs(sfp="frozen"):
    # a busy screen (no prompt) with a heartbeat region present.
    return Observation(prompt_kind="none", semantic_fp=sfp,
                       heartbeat=Heartbeat("h", True, True))


def test_hook_active_ignores_screen_idle():
    # hook says busy; a screen frame that WOULD otherwise settle to a free-text idle must publish
    # nothing while hook-active (hooks own the state; only the bounded reconciliation may speak).
    # Advance past idle_confirm_window (but stay well within hook_turn_grace) so a screen-only engine
    # WOULD have published waiting_for_user here — proving the suppression, not just an unsettled edge.
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.on_hook(H("working", opens=True), 0.0)              # hook_active, busy
    assert e.tick(idle_obs(), CTX) == []                  # idle edge
    clk.advance(2.0)                                      # settled by screen rules, still < hook_turn_grace
    assert e.tick(idle_obs(), CTX) == []                  # hook-active -> still nothing published
    assert e.state.control_state == "busy" and e.hook_mode == "active"


def test_lost_stop_times_out_to_intervention():
    # a busy hook turn whose subsequent hooks are all lost and whose screen meaning never advances:
    # past lost_stop_after this is a stuck agent (not silently idle) -> intervention_required.
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.on_hook(H("working", opens=True), 0.0)              # hook_active, busy, last hook at t=0
    clk.advance(50.0)                                     # no further hook; past lost_stop_after (45)
    acts = e.tick(busy_obs(), CTX)                        # busy screen, meaning frozen since the hook
    assert any(isinstance(a, Publish) and a.kind == "intervention_required" for a in acts)
    assert e.state.control_state == "intervention_required"


def test_lost_stop_free_text_reconciles_to_idle():
    # the agent finished (screen settled to a stable free-text prompt) but the Stop hook was lost:
    # past hook_turn_grace the screen reconciles to a low-confidence idle (reconciled=True), never
    # a respondable waiting_for_user.
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.on_hook(H("working", opens=True), 0.0)             # busy via hook
    ft = idle_obs(sfp="done")
    clk.advance(1.0)
    assert e.tick(ft, CTX) == []                          # free-text edge, not stable long enough
    clk.advance(5.0)                                      # past hook_turn_grace (4)
    acts = e.tick(ft, CTX)
    pubs = [a for a in acts if isinstance(a, Publish)]
    assert pubs and pubs[0].kind == "idle" and pubs[0].payload.get("reconciled") is True
    assert not any(p.kind == "waiting_for_user" for p in pubs)
    assert e.state.control_state == "idle"


def test_lost_stop_nags_then_a_new_hook_recovers_to_busy():
    # the lost-Stop intervention re-fires as a nag (incrementing count) while still stuck; a fresh
    # hook (the agent resumed reporting) resets the lost-hook clocks and returns control to busy.
    clk = FakeClock(0.0)
    cfg = BeliefConfig(lost_stop_after=10.0, live_budget=10.0, heartbeat_stale_after=100.0)
    e = BeliefEngine(cfg, clk)
    e.on_hook(H("working", opens=True), 0.0)
    fired = []
    for _ in range(40):                                  # 40s of a frozen busy screen, no hooks
        clk.advance(1.0)
        fired += [a for a in e.tick(busy_obs(), CTX)
                  if isinstance(a, Publish) and a.kind == "intervention_required"]
    counts = [a.payload["escalation_count"] for a in fired]
    assert counts == sorted(counts) and counts and counts[-1] >= 2   # nags with incrementing count
    assert e.state.control_state == "intervention_required"
    # a new hook (agent resumed) -> back to busy, lost-hook clocks reset
    e.on_hook(H("working"), clk.now())
    assert e.state.control_state == "busy"


def test_process_exit_beats_stale_hook():
    # process exit / crash is the highest precedence: a dead child finalizes regardless of the last
    # hook state (hook said busy).
    e = eng()
    e.on_hook(H("working", opens=True), 0.0)
    acts = e.tick(Observation(prompt_kind="exit"),
                  ObservationCtx(last_submitted_text=None, child_alive=False, exit_code=0))
    assert any(isinstance(a, Finalize) for a in acts)
    assert e.state.control_state == "terminal"


def test_unknown_grace_suppresses_screen_idle():
    # a hook-capable session whose first hook has not arrived yet (hook_mode "unknown", within the
    # startup grace): the screen must NOT declare a free-text idle while a hook may still be coming.
    e = eng()
    e.expect_hooks(0.0)                                   # hook-capable, task delivered, no hook yet
    assert e.tick(idle_obs(), CTX) == []                 # conservative: no screen idle during grace
    assert e.hook_mode == "unknown"


def test_unknown_expires_to_unavailable_then_screen_path_as_today():
    # a hook-capable session that NEVER gets a hook transitions "unknown" -> "unavailable" after the
    # startup grace and then behaves EXACTLY as today (screen-driven waiting_for_user publishes).
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.expect_hooks(0.0)
    clk.advance(0.5)
    assert e.tick(idle_obs(), CTX) == []                 # within grace -> suppressed
    assert e.hook_mode == "unknown"
    clk.advance(13.0)                                     # past hook_startup_grace (12)
    acts = e.tick(idle_obs(), CTX)                        # grace expired -> unavailable, candidate settled
    assert e.hook_mode == "unavailable"
    assert any(isinstance(a, Publish) and a.kind == "waiting_for_user" for a in acts)


def test_screen_only_session_unaffected_no_expect_hooks():
    # the pure screen path (no hooks expected, never armed) is untouched: a settled free-text idle
    # publishes waiting_for_user exactly as before (regression guard for the whole existing suite).
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    assert e.tick(busy_obs(), CTX) == []
    clk.advance(0.1)
    assert e.tick(idle_obs(), CTX) == []                 # idle edge, not settled
    clk.advance(2.0)
    acts = e.tick(idle_obs(), CTX)
    assert any(isinstance(a, Publish) and a.kind == "waiting_for_user" for a in acts)
    assert e.hook_mode == "unknown"                      # never armed, never flipped
