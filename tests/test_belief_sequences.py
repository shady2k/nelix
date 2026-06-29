"""Synthetic timed sequence tests (spec §9 tier 2): one minimal timed sequence per belief rule,
driven through the pure BeliefEngine with a FakeClock — about transitions over time, not single
observe() calls."""
from daemon.belief import BeliefEngine, Publish, Withdraw
from daemon.clock import FakeClock
from daemon.observation import Observation, ObservationCtx, Heartbeat
from daemon.config import BeliefConfig

CTX = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)


def _pubs(actions):
    return [a for a in actions if isinstance(a, Publish)]


# ---- Task 10: post-submit suppression of false idle (spec §7.1, fixes F1) ----

def test_post_submit_gap_does_not_publish():
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.on_submit("Good. After the T6 commit lands, proceed with T7...")   # enters post_submit_ttft
    ctx = ObservationCtx("Good. After the T6 commit lands, proceed with T7...", True, None)
    echo = Observation(prompt_kind="free_text", submitted_echo_present=True,
                       semantic_fp="echo", affordances=frozenset({"accepts_text_input"}))
    for _ in range(40):                       # 4s of stable echo-in-box, no spinner
        clk.advance(0.1)
        assert e.tick(echo, ctx) == []        # MUST NOT publish during the gap
    work = Observation(prompt_kind="none", semantic_fp="w", heartbeat=Heartbeat("h", True, True))
    assert e.tick(work, ctx) == []            # cleared by working, still nothing


def test_modal_during_ttft_bypasses_suppression():
    # An immediate legitimate question right after submit must NOT be swallowed (IMPORTANT-9, P3).
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.on_submit("do the thing")
    modal = Observation(prompt_kind="modal_choice", semantic_fp="menu",
                        affordances=frozenset({"modal_choice"}))
    clk.advance(0.6)                          # past the confirm window, still inside grace
    acts = e.tick(modal, CTX)
    assert any(p.kind == "waiting_for_user" for p in _pubs(acts))   # surfaced immediately


def test_grace_expiry_then_real_idle_publishes():
    # After the bounded grace, a genuine idle (echo gone, content advanced) publishes.
    clk = FakeClock(0.0)
    cfg = BeliefConfig(post_submit_grace=2.0)
    e = BeliefEngine(cfg, clk)
    e.on_submit("answer text")
    echo = Observation(prompt_kind="free_text", submitted_echo_present=True, semantic_fp="e",
                       affordances=frozenset({"accepts_text_input"}))
    clk.advance(1.0)
    assert e.tick(echo, ObservationCtx("answer text", True, None)) == []   # suppressed in grace
    # the agent answered: echo gone, content changed -> a fresh idle prompt
    idle = Observation(prompt_kind="free_text", submitted_echo_present=False, semantic_fp="done",
                       affordances=frozenset({"accepts_text_input"}))
    clk.advance(3.0)                          # past grace
    e.tick(idle, CTX)                         # idle edge
    clk.advance(1.0)
    acts = e.tick(idle, CTX)                  # settled, grace gone, echo gone -> publish
    assert _pubs(acts), "a real idle after grace must publish"


# ---- Task 11: revocable decisions + auto-recovery + anti-flap (spec §7.2) ----

def _idle(sfp="q", pfp="pq", hb=None):
    return Observation(prompt_kind="free_text", semantic_fp=sfp, prompt_fp=pfp,
                       affordances=frozenset({"accepts_text_input"}),
                       heartbeat=hb or Heartbeat())


def _publish_idle(e, clk):
    e.tick(_idle(), CTX)
    clk.advance(1.0)
    return e.tick(_idle(), CTX)


def test_withdraw_on_turn_resumption():
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    assert _pubs(_publish_idle(e, clk))
    work = Observation(prompt_kind="none", semantic_fp="w", heartbeat=Heartbeat("h", True, True))
    clk.advance(0.5)
    acts = e.tick(work, CTX)                   # agent resumed, no respond -> withdraw
    assert any(isinstance(a, Withdraw) for a in acts)
    assert e.state.control_state == "busy"


def test_fresh_heartbeat_alone_does_not_withdraw():
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.tick(_idle(hb=Heartbeat("hb1", True, True)), CTX)
    clk.advance(1.0)
    assert _pubs(e.tick(_idle(hb=Heartbeat("hb1", True, True)), CTX))
    clk.advance(0.5)
    # same prompt (same prompt_fp), only the footer heartbeat ticked -> MUST NOT withdraw (IMPORTANT-8)
    acts = e.tick(_idle(hb=Heartbeat("hb2", True, True)), CTX)
    assert not any(isinstance(a, Withdraw) for a in acts)
    assert e.state.control_state == "awaiting_user"


def test_anti_flap_same_fp_within_cooldown():
    cfg = BeliefConfig(withdrawn_cooldown=2.0)
    clk = FakeClock(0.0)
    e = BeliefEngine(cfg, clk)
    assert _pubs(_publish_idle(e, clk))                 # publish at t=1.0
    work = Observation(prompt_kind="none", semantic_fp="w", heartbeat=Heartbeat("h", True, True))
    clk.advance(0.5)
    assert any(isinstance(a, Withdraw) for a in e.tick(work, CTX))   # withdraw at t=1.5
    # the SAME idle fingerprint reappears within cooldown (until 3.5) -> NOT re-minted
    clk.advance(0.5)
    e.tick(_idle(), CTX)                                  # candidate at t=2.0
    clk.advance(1.0)
    assert not _pubs(e.tick(_idle(), CTX))               # t=3.0, settled but within cooldown
    clk.advance(1.0)
    assert _pubs(e.tick(_idle(), CTX))                   # t=4.0, cooldown elapsed -> re-publish


def test_positive_turn_start_clears_suppression():
    # (a) a busy observation with positive liveness clears post-submit -> next real idle publishes.
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.on_submit("go")
    work = Observation(prompt_kind="none", semantic_fp="w1", heartbeat=Heartbeat("h1", True, True))
    e.tick(work, CTX)
    clk.advance(0.5)
    work2 = Observation(prompt_kind="none", semantic_fp="w2", heartbeat=Heartbeat("h2", True, True))
    e.tick(work2, CTX)                        # live heartbeat -> turn started -> suppression cleared
    idle = Observation(prompt_kind="free_text", submitted_echo_present=False, semantic_fp="idle",
                       affordances=frozenset({"accepts_text_input"}))
    clk.advance(0.5)
    e.tick(idle, CTX)
    clk.advance(1.0)
    assert _pubs(e.tick(idle, CTX)), "post-turn-start idle should publish"
