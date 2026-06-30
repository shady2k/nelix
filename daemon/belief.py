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
        # watchdog / nag (spec §7.4/7.5): a frozen-meaning busy screen past the liveness-scaled budget
        # escalates a NON-respondable intervention_required, re-firing each budget while still stuck.
        self._intervention_active = False
        self._escalation_count = 0
        self._next_nag_at = None
        # post-submit suppression (spec §7.1): while active, an ambiguous free_text idle is NOT
        # published as a wake (the just-submitted echo lingering in the box during TTFT is not idle).
        self._post_submit_active = False
        self._post_submit_grace_until = None
        self._post_submit_content_fp = None   # content_fp captured on the first post-submit tick
        # stuck-input bound (spec §7.1, fixes nelix-sud): when our just-submitted answer's Enter does
        # not land, the echo stays in the box indefinitely; `_echo_since` clocks how long it has held
        # so a never-clearing box surfaces a wake instead of suppressing every wake forever.
        self._echo_since = None
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
        self._echo_since = None                  # a fresh submit: re-clock the stuck-input bound

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
            # meaning advanced -> the stuck episode ends: reset the watchdog/nag ladder.
            self._intervention_active = False
            self._escalation_count = 0
            self._next_nag_at = None

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
        self._echo_since = None                  # the box cleared / turn moved: stuck-input bound off
        self._state.phase = "busy"
        self._watchdog(obs, now, actions)

    def _budget(self, liveness, busy_reason):
        # Liveness-scaled (spec §7.4): long while `live`, short while `stale`/`unknown`. A known
        # long-running reason (silent shell command / subagents) earns the long budget even when
        # liveness is `unknown` (a quiet shell never animates), so it is not falsely escalated.
        if liveness == "stale":
            return self._cfg.stale_budget
        if liveness == "live":
            return self._cfg.live_budget
        if busy_reason in ("running_command", "waiting_subagents"):
            return self._cfg.live_budget
        return self._cfg.unknown_budget

    def _watchdog(self, obs, now, actions):
        # The ladder: semantic changed recently -> ok; else (frozen meaning) escalate once the
        # liveness-scaled budget elapses, re-firing each budget as a nag while still stuck. The daemon
        # NEVER acts on the agent (no ESC) — it only surfaces the advisory; the orchestrator decides.
        quiet = 0.0 if self._semantic_change_ts is None else now - self._semantic_change_ts
        budget = self._budget(self._liveness(obs, now), obs.busy_reason)
        if quiet < budget:
            self._intervention_active = False
            return
        self._intervention_active = True
        if self._next_nag_at is None or now >= self._next_nag_at:
            self._escalation_count += 1
            self._next_nag_at = now + budget
            self._state.phase = "suspected_hung"
            actions.append(Publish(
                kind="intervention_required", respondable=False,
                decision_key=f"intervention:{self._escalation_count}",
                payload={"escalation_count": self._escalation_count,
                         "busy_reason": obs.busy_reason,
                         "liveness": self._liveness(obs, now),
                         "hint": "suspected_hung"}))

    def _escalate_stuck_input(self, obs, now, actions):
        # A submitted answer whose Enter never landed: the box is frozen holding our text and will not
        # clear on its own (spec §7.1, fixes nelix-sud). Surface a NON-respondable needs-attention
        # advisory — re-respond would just double-type into the still-full box, so the safe recovery is
        # restart, which the orchestrator drives off this wake. Mirrors _watchdog's nag throttle
        # (shared escalation counter, reset by real semantic progress in _track_semantic).
        self._intervention_active = True
        budget = self._budget(self._liveness(obs, now), obs.busy_reason)
        if self._next_nag_at is None or now >= self._next_nag_at:
            self._escalation_count += 1
            self._next_nag_at = now + budget
            self._state.phase = "submit_unconfirmed"
            actions.append(Publish(
                kind="intervention_required", respondable=False,
                decision_key=f"intervention:{self._escalation_count}",
                payload={"escalation_count": self._escalation_count,
                         "busy_reason": obs.busy_reason,
                         "liveness": self._liveness(obs, now),
                         "hint": "submit_unconfirmed"}))

    def _on_prompt(self, obs, now, actions):
        self._intervention_active = False         # a prompt is not a hang: clear any nag state
        # Auto-recovery: a published decision whose prompt region CHANGED AWAY (different prompt_fp)
        # is a stale hypothesis -> withdraw it (a fresh heartbeat alone never withdraws — that is the
        # footer timer, not a turn change, IMPORTANT-8). The new prompt is then a fresh candidate.
        if self._published_key is not None and obs.prompt_fp != self._published_prompt_fp:
            self._withdraw(actions, "prompt_changed", now)

        # Re-mint backstop (spec §7.2): if a decision is STILL published here, its prompt_fp matched
        # the published one (else the block above just withdrew it) -> the SAME respondable prompt is
        # still on screen, unanswered. Do NOT re-publish on semantic_fp churn: the TUI repaints the
        # scrolled conversation row-by-row, so the whole-frame meaning flickers while the bottom-
        # anchored ❯ box is held stable. _published_key only persists while Claude sits CONTINUOUSLY
        # at a prompt (a turn_resumed clears it in _on_busy, on_submit clears it on answer), during
        # which a single foreground turn cannot change its pending question without an answer first.
        # So once a respondable prompt is published it holds until its region changes / goes busy /
        # terminal / is answered. (_refresh_state still reports awaiting_user while it is set.)
        if self._published_key is not None:
            self._state.phase = "pause_candidate"
            return

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
        # free_text post-submit suppression (spec §7.1, fixes F1):
        #  - while our submission is STILL in the active input box, suppress — it is not an idle
        #    prompt, regardless of the grace (the box literally holds our text);
        #  - once the echo is gone, the bounded grace still suppresses the TTFT gap (no spinner yet).
        # BUT the echo suppression is BOUNDED (fixes nelix-sud): if our answer's Enter never landed
        # (executor mid-render / misread the keystroke) the echo would otherwise hold forever and
        # suppress every wake into infinite silence. Past echo_stuck_after the box is not clearing on
        # its own — surface a needs-attention advisory so the orchestrator recovers (re-respond /
        # restart) instead of the executor sitting idle, silently, indefinitely.
        if obs.submitted_echo_present:
            if self._echo_since is None:
                self._echo_since = now
            if (now - self._echo_since) < self._cfg.echo_stuck_after:
                return
            self._escalate_stuck_input(obs, now, actions)
            return
        self._echo_since = None                       # echo gone: the submit landed, clear the clock
        if self._post_submit_active and now < self._post_submit_grace_until:
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
        elif self._intervention_active:
            self._state.control_state = "intervention_required"
        else:
            self._state.control_state = "busy"
        self._state.quiet_elapsed = (0.0 if self._semantic_change_ts is None
                                     else now - self._semantic_change_ts)
        self._state.liveness = self._liveness(obs, now)
        self._state.busy_reason = obs.busy_reason
        self._state.escalation_count = self._escalation_count
