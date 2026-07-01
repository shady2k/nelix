"""BeliefEngine.on_hook — the authoritative hook-driven state path (plan Task 6).

Mirrors test_belief_engine.py's style. These exercise the additive `on_hook` entry only (the
screen `tick` path is untouched here; precedence/reconciliation is Task 7). A hook is ground
truth: the first hook flips `hook_mode` to "active", `UserPromptSubmit` opens a fresh turn epoch,
and `Stop` publishes the new non-respondable `idle` decision that keeps the session alive.
"""
from daemon.belief import BeliefEngine, Publish, Withdraw
from daemon.hooks import HookObservation
from daemon.clock import FakeClock
from daemon.config import BeliefConfig


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


def test_late_posttooluse_after_stop_is_ignored():
    e = eng()
    e.on_hook(H("idle", closes=True), 0.0)
    assert e.on_hook(H("working"), 0.1) == []            # no new turn -> ignored, stays idle
    assert e.state.control_state == "idle"


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
