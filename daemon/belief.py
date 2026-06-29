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
        # idle candidate (contiguous prompt observation)
        self._candidate_fp = None         # the prompt_fp of the current candidate
        self._candidate_since = None
        # currently published decision
        self._published_key = None
        self._published_kind = None
        # state snapshot
        self._state = EngineState()

    @property
    def state(self):
        return self._state

    # ---- submit edge (post-submit suppression entry, spec §7.1) ----
    def on_submit(self, text):
        # A positive turn-start edge: the agent just received our text. Forget any published decision
        # (it was answered / will be superseded) and reset the idle candidate so the echoed submission
        # lingering in the box during TTFT is not read as a fresh idle prompt. Task 10 extends this
        # with the post-submit grace window.
        self._published_key = None
        self._published_kind = None
        self._candidate_fp = None
        self._candidate_since = None

    # ---- main entry ----
    def tick(self, obs, ctx):
        now = self._clock.now()
        actions = []
        self._track_semantic(obs, now)

        # Terminal: child gone, or the driver read a crash/exit screen.
        if not ctx.child_alive or obs.prompt_kind in ("crash", "exit"):
            self._state.control_state = "terminal"
            self._state.phase = "terminal"
            actions.append(Finalize())
            return actions

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

    def _on_busy(self, obs, now, actions):
        # No prompt visible -> reset the idle candidate; the agent is working.
        self._candidate_fp = None
        self._candidate_since = None
        self._state.phase = "busy"

    def _on_prompt(self, obs, now, actions):
        # Track the contiguous idle/prompt candidate by its prompt fingerprint.
        key = obs.prompt_fp or obs.semantic_fp
        if key != self._candidate_fp:
            self._candidate_fp = key
            self._candidate_since = now
        self._state.phase = "pause_candidate"

        decision_key = f"{obs.prompt_kind}:{obs.semantic_fp}"
        if self._published_key == decision_key:
            return                                    # same decision already published
        settled = (now - self._candidate_since) >= self._cfg.idle_confirm_window
        if not settled:
            return
        self._publish_decision(obs, decision_key, actions)

    def _publish_decision(self, obs, decision_key, actions):
        self._published_key = decision_key
        self._published_kind = obs.prompt_kind
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
        self._state.busy_reason = None
