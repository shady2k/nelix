from daemon.belief import BeliefEngine, Publish, Withdraw, Finalize, Actuate
from daemon.clock import FakeClock
from daemon.observation import Observation, ObservationCtx, Heartbeat
from daemon.config import BeliefConfig

CTX = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)


def busy(fp="h1"):
    return Observation(prompt_kind="none", semantic_fp="work",
                       heartbeat=Heartbeat(fp, True, True))


def idle():
    return Observation(prompt_kind="free_text", semantic_fp="idle",
                       affordances=frozenset({"accepts_text_input"}))


def test_stable_idle_publishes_waiting_for_user_once():
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    assert e.tick(busy(), CTX) == []                  # working, no decision
    clk.advance(0.1)
    assert e.tick(idle(), CTX) == []                  # idle edge just appeared, not settled yet
    clk.advance(2.0)
    acts = e.tick(idle(), CTX)                         # settled -> publish
    assert any(isinstance(a, Publish) and a.kind == "waiting_for_user" and a.respondable
               for a in acts)
    clk.advance(1.0)
    assert e.tick(idle(), CTX) == []                  # same decision, no re-publish


def test_decision_key_is_prompt_kind_and_semantic_fp():
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.tick(busy(), CTX)
    clk.advance(0.1)
    e.tick(idle(), CTX)                               # idle edge
    clk.advance(2.0)
    acts = e.tick(idle(), CTX)                        # settled -> publish
    pub = [a for a in acts if isinstance(a, Publish)][0]
    assert pub.decision_key == "free_text:idle"


def test_busy_only_never_publishes():
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    for _ in range(50):
        clk.advance(1.0)
        assert e.tick(busy(), CTX) == []


def test_state_snapshot_exposes_control_state():
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.tick(busy(), CTX)
    assert e.state.control_state == "busy"
    clk.advance(0.1)
    e.tick(idle(), CTX)
    clk.advance(2.0)
    e.tick(idle(), CTX)
    assert e.state.control_state == "awaiting_user"


def test_child_exit_finalizes():
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    ctx = ObservationCtx(last_submitted_text=None, child_alive=False, exit_code=0)
    obs = Observation(prompt_kind="exit")
    acts = e.tick(obs, ctx)
    assert any(isinstance(a, Finalize) for a in acts)


def test_action_types_importable():
    # the four Action types exist and carry their documented fields
    p = Publish(kind="waiting_for_user", respondable=True, decision_key="k", payload={})
    w = Withdraw(decision_key="k", reason="turn_resumed")
    a = Actuate(kind="select_option", arg="1")
    assert p.respondable is True and w.reason == "turn_resumed" and a.arg == "1"
    assert isinstance(Finalize(), Finalize)


# ---- Task 9: three-valued liveness from the heartbeat timestamps (spec §7.4) ----

def _hb(fp):
    return Observation(prompt_kind="none", semantic_fp="work",
                       heartbeat=Heartbeat(fp, True, True))


def test_liveness_live_when_heartbeat_fp_changes():
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.tick(_hb("h1"), CTX)
    clk.advance(1.0)
    e.tick(_hb("h2"), CTX)                  # heartbeat fp changed -> animating -> live
    assert e.state.liveness == "live"


def test_liveness_stale_when_heartbeat_frozen_but_expected_to_change():
    cfg = BeliefConfig(heartbeat_stale_after=5.0)
    clk = FakeClock(0.0)
    e = BeliefEngine(cfg, clk)
    e.tick(_hb("h1"), CTX)
    clk.advance(2.0)
    e.tick(_hb("h1"), CTX)                  # frozen but not long enough yet
    assert e.state.liveness == "live"
    clk.advance(6.0)
    e.tick(_hb("h1"), CTX)                  # frozen past the stale budget -> stale
    assert e.state.liveness == "stale"


def test_liveness_unknown_when_no_heartbeat_region():
    clk = FakeClock(0.0)
    e = BeliefEngine(BeliefConfig(), clk)
    e.tick(Observation(prompt_kind="none", semantic_fp="x",
                       heartbeat=Heartbeat(present=False)), CTX)
    assert e.state.liveness == "unknown"


def test_liveness_unknown_for_static_heartbeat_not_expected_to_change():
    # present but NOT expected to change (e.g. a silent shell command) -> unknown, never stale.
    cfg = BeliefConfig(heartbeat_stale_after=1.0)
    clk = FakeClock(0.0)
    e = BeliefEngine(cfg, clk)
    obs = Observation(prompt_kind="none", semantic_fp="x",
                      heartbeat=Heartbeat("h1", True, expected_to_change=False))
    e.tick(obs, CTX)
    clk.advance(10.0)
    e.tick(obs, CTX)
    assert e.state.liveness == "unknown"
