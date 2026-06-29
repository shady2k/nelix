"""BeliefEngine — the pure, single home of temporal interpretation (spec §5.7, §7).

`Session` pumps/renders, calls `driver.observe()`, feeds the engine, and applies the emitted
actions. The engine owns ALL temporal memory (timestamps, idle candidate, liveness, post-submit
suppression, anti-flap, watchdog/nag) behind an injected clock — no `time.*` calls here. This is
what keeps the implemented core minimal (P4) and makes the replay harness deterministic (§9).
"""
from dataclasses import dataclass, field
from typing import Optional


# ---- Actions the engine emits; Session applies them (it owns every PTY write) ----

@dataclass
class Publish:
    kind: str                 # waiting_for_user | intervention_required
    respondable: bool
    decision_key: str         # stable identity of this pause (REUSED across re-emits)
    payload: dict = field(default_factory=dict)


@dataclass
class Withdraw:
    decision_key: str
    reason: str               # turn_resumed | prompt_left | superseded


@dataclass
class Finalize:
    pass


@dataclass
class Actuate:
    kind: str                 # select_option | submit_text | interrupt
    arg: Optional[str] = None


# ---- Read-only state snapshot exposed for /status, the trail, and the test oracle ----

@dataclass
class EngineState:
    control_state: str = "busy"           # busy | awaiting_user | intervention_required | terminal
    busy_reason: Optional[str] = None
    liveness: str = "unknown"             # live | stale | unknown
    quiet_elapsed: float = 0.0
    escalation_count: int = 0
    phase: str = "starting"


_PROMPT_KINDS = ("free_text", "modal_choice", "permission_choice")


class BeliefEngine:
    def __init__(self, cfg, clock):
        self._cfg = cfg
        self._clock = clock
        # fingerprint timestamps
        self._semantic_fp = None
        self._semantic_change_ts = None
        # heartbeat timestamps (spec §7.4 / IMPORTANT-10): seen whenever present, changed only on fp
        # change. Three-valued liveness is derived from these + the driver's expected_to_change flag.
        self._heartbeat_fp = None
        self._heartbeat_seen_ts = None
        self._heartbeat_changed_ts = None
        # idle candidate (contiguous prompt observation)
        self._candidate_fp = None         # the prompt_fp of the current candidate
        self._candidate_since = None
        # currently published decision
        self._published_key = None
        self._published_kind = None
        self._published_prompt_fp = None      # prompt region fp at publish (detects "prompt changed")
        self._published_semantic_fp = None    # semantic fp at publish (anti-flap key)
        # anti-flap (spec §7.2): don't re-mint the same semantic_fp immediately after withdrawing it.
        self._last_withdrawn_fp = None
        self._withdrawn_cooldown_until = None
        # post-submit suppression (spec §7.1): while active, an ambiguous free_text idle is NOT
        # published as a wake (the just-submitted echo lingering in the box during TTFT is not idle).
        self._post_submit_active = False
        self._post_submit_grace_until = None
        self._post_submit_content_fp = None   # content_fp captured on the first post-submit tick
        # state snapshot
        self._state = EngineState()

    @property
    def state(self):
        return self._state

    # ---- submit edge (post-submit suppression entry, spec §7.1) ----
    def on_submit(self, text):
        # The agent just received our text. Forget any published decision (it was answered / will be
        # superseded), reset the idle candidate, and ENTER post_submit_ttft: while active, an
        # ambiguous free_text idle is suppressed (the echoed submission is not a fresh prompt, F1).
        now = self._clock.now()
        self._published_key = None
        self._published_kind = None
        self._candidate_fp = None
        self._candidate_since = None
        self._published_prompt_fp = None
        self._published_semantic_fp = None
        self._last_withdrawn_fp = None          # a fresh turn: clear anti-flap from the prior turn
        self._withdrawn_cooldown_until = None
        self._post_submit_active = True
        self._post_submit_grace_until = now + self._cfg.post_submit_grace
        self._post_submit_content_fp = None

    # ---- main entry ----
    def tick(self, obs, ctx):
        now = self._clock.now()
        actions = []
        self._track_semantic(obs, now)
        self._track_heartbeat(obs, now)

        # Terminal: child gone, or the driver read a crash/exit screen.
        if not ctx.child_alive or obs.prompt_kind in ("crash", "exit"):
            self._state.control_state = "terminal"
            self._state.phase = "terminal"
            actions.append(Finalize())
            return actions

        self._update_post_submit(obs, now)
        if obs.prompt_kind in _PROMPT_KINDS:
            self._on_prompt(obs, now, actions)
        else:
            self._on_busy(obs, now, actions)

        self._refresh_state(obs, now)
        return actions

    # ---- substrate ----
    def _track_semantic(self, obs, now):
        if obs.semantic_fp != self._semantic_fp:
            self._semantic_fp = obs.semantic_fp
            self._semantic_change_ts = now

    def _track_heartbeat(self, obs, now):
        hb = obs.heartbeat
        if not hb.present:
            return                                  # no region this frame: keep prior timestamps
        self._heartbeat_seen_ts = now
        if hb.fp != self._heartbeat_fp:
            self._heartbeat_fp = hb.fp
            self._heartbeat_changed_ts = now

    def _liveness(self, obs, now):
        # Three-valued (spec §7.4): live (heartbeat changed recently) / stale (present & expected to
        # change but frozen past the budget) / unknown (no region, or static & not expected to tick).
        hb = obs.heartbeat
        if not hb.present or not hb.expected_to_change:
            return "unknown"
        if self._heartbeat_changed_ts is None:
            return "unknown"
        if (now - self._heartbeat_changed_ts) < self._cfg.heartbeat_stale_after:
            return "live"
        return "stale"

    def _update_post_submit(self, obs, now):
        # Clear post-submit suppression on a positive turn-start signal (spec §7.1 a-d). Until then
        # an ambiguous free_text idle is suppressed in _on_prompt.
        if not self._post_submit_active:
            return
        if self._post_submit_content_fp is None:
            self._post_submit_content_fp = obs.content_fp     # baseline (our echo, not real output)
        if now >= self._post_submit_grace_until:              # (d) bounded grace expired
            self._post_submit_active = False
            return
        if obs.prompt_kind == "none" and self._liveness(obs, now) == "live":   # (a) busy + live
            self._post_submit_active = False
            return
        if (not obs.submitted_echo_present
                and obs.content_fp != self._post_submit_content_fp):           # (b) real output
            self._post_submit_active = False
            return
        # (c) child exit/crash is handled by the terminal branch in tick().

    def _on_busy(self, obs, now, actions):
        # No prompt visible -> the turn resumed. Withdraw a still-pending decision (auto-recovery,
        # spec §7.2) and reset the idle candidate; the agent is working.
        if self._published_key is not None:
            self._withdraw(actions, "turn_resumed", now)
        self._candidate_fp = None
        self._candidate_since = None
        self._state.phase = "busy"

    def _on_prompt(self, obs, now, actions):
        # Auto-recovery: a published decision whose prompt region CHANGED AWAY (different prompt_fp)
        # is a stale hypothesis -> withdraw it (a fresh heartbeat alone never withdraws — that is the
        # footer timer, not a turn change, IMPORTANT-8). The new prompt is then a fresh candidate.
        if self._published_key is not None and obs.prompt_fp != self._published_prompt_fp:
            self._withdraw(actions, "prompt_changed", now)

        # Track the contiguous idle/prompt candidate by its prompt fingerprint.
        key = obs.prompt_fp or obs.semantic_fp
        if key != self._candidate_fp:
            self._candidate_fp = key
            self._candidate_since = now
        self._state.phase = "pause_candidate"

        decision_key = f"{obs.prompt_kind}:{obs.semantic_fp}"
        if self._published_key == decision_key:
            return                                    # same decision already published
        # A modal/permission prompt is high-confidence: it bypasses post-submit suppression AND the
        # confirm window and publishes promptly (P3, IMPORTANT-9) — an explicit question is never
        # swallowed, even during TTFT.
        immediate = obs.prompt_kind in ("modal_choice", "permission_choice")
        if immediate:
            self._publish_decision(obs, decision_key, actions)
            return
        # free_text: suppress while post-submit is active (echo lingering / within grace).
        if self._post_submit_active and (obs.submitted_echo_present
                                         or now < self._post_submit_grace_until):
            return
        # anti-flap: do not re-mint the SAME semantic_fp within the cooldown after withdrawing it.
        if (obs.semantic_fp == self._last_withdrawn_fp
                and self._withdrawn_cooldown_until is not None
                and now < self._withdrawn_cooldown_until):
            return
        settled = (now - self._candidate_since) >= self._cfg.idle_confirm_window
        if not settled:
            return
        self._publish_decision(obs, decision_key, actions)

    def _withdraw(self, actions, reason, now):
        actions.append(Withdraw(decision_key=self._published_key, reason=reason))
        self._last_withdrawn_fp = self._published_semantic_fp
        self._withdrawn_cooldown_until = now + self._cfg.withdrawn_cooldown
        self._published_key = None
        self._published_kind = None
        self._published_prompt_fp = None
        self._published_semantic_fp = None

    def _publish_decision(self, obs, decision_key, actions):
        self._published_key = decision_key
        self._published_kind = obs.prompt_kind
        self._published_prompt_fp = obs.prompt_fp
        self._published_semantic_fp = obs.semantic_fp
        hint = "needs_permission" if obs.prompt_kind == "permission_choice" else None
        payload = {"prompt_kind": obs.prompt_kind,
                   "options": obs.options,
                   "busy_reason": None,
                   "hint": hint}
        actions.append(Publish(kind="waiting_for_user", respondable=True,
                               decision_key=decision_key, payload=payload))

    def _refresh_state(self, obs, now):
        if self._published_key is not None:
            self._state.control_state = "awaiting_user"
        else:
            self._state.control_state = "busy"
        self._state.quiet_elapsed = (0.0 if self._semantic_change_ts is None
                                     else now - self._semantic_change_ts)
        self._state.liveness = self._liveness(obs, now)
        self._state.busy_reason = obs.busy_reason
