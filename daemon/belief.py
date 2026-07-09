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


@dataclass
class Note:
    """A pure DIAGNOSTIC the engine surfaces about WHY it did (or did not) act — the suppression
    rationale and post-submit window edges (spec §7.1). Carried on a side-channel buffer drained by
    Session (drain_notes), NEVER on tick()'s action list: actuation is unchanged, and the engine's
    action-equality tests keep asserting `== []`. Session maps it to a structured log record."""
    event: str
    fields: dict = field(default_factory=dict)


# ---- Read-only state snapshot exposed for /status, the trail, and the test oracle ----

@dataclass
class EngineState:
    control_state: str = "busy"           # busy | awaiting_user | idle | intervention_required | terminal
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
        # diagnostic note buffer (nelix-jwv): WHY we suppressed / post-submit edges, edge-triggered so
        # a persistent reason logs once (signal, not per-tick noise). Drained by Session, off `actions`.
        self._notes = []
        self._suppress_reason = None     # last emitted suppression reason (edge detection)
        # hook path (Task 6): a Claude agent that reports its own lifecycle via hooks is ground
        # truth. The FIRST hook flips hook_mode "unknown"->"active"; turns are counted so a decision
        # is scoped to its epoch (idle idempotent within a turn, a fresh UserPromptSubmit re-opens).
        self._hook_mode = "unknown"       # unknown | active | unavailable
        self._turn_epoch = 0              # bumped by each opens_turn (UserPromptSubmit)
        self._turn_open = False           # a turn is running (between open and its close)
        self._hook_pending_key = None     # currently-published hook waiting_for_user, for withdrawal
        self._hook_pending_kind = None    # its effective prompt_kind (modal_choice wins, nelix-32f)
        # nelix-f7y: the set of hook-pause keys ANSWERED in the current epoch (a per-gate tombstone).
        # A pause is keyed by a per-gate FINGERPRINT of its tool_name + tool_input (hooks.gate_fp): both
        # hooks of ONE physical gate carry identical tool_input -> same key (dedup), different gates
        # differ -> distinct keys. So a late duplicate / straggler of an answered gate reuses the SAME
        # key -> suppressed (no phantom re-publish), while a genuinely new gate has a DIFFERENT key ->
        # published (answerable). No epoch-level ambiguity and no hook-ordering / tool-progress
        # assumption. Cleared at each epoch boundary. See on_submit + the waiting_for_user branch.
        # A repeated IDENTICAL gate (an allow-once approval then re-running the same command) shares its
        # predecessor's fingerprint, so its tombstone is LIFTED by the fresh working PreToolUse that
        # precedes its PermissionRequest (a new-invocation signal — see the working-hook branch).
        # RESIDUALS: (1) a waiting_for_user hook with NO tool_input — only the legacy permission
        # Notification path; PermissionRequest / PreToolUse[AskUserQuestion] both carry it — falls back
        # to the epoch key, which cannot distinguish a second same-kind gate from a straggler in one
        # epoch. (2) a repeated identical gate whose PermissionRequest arrives BEFORE any new-invocation
        # PreToolUse signal for that gate — a Bash gate whose second PermissionRequest is reordered ahead
        # of its own PreToolUse, or a repeated identical AskUserQuestion (whose PreToolUse IS the pause,
        # not a working signal) — is genuinely indistinguishable from a straggler of the answered gate
        # and is suppressed. Truly minimal without a per-gate id, which Claude's PermissionRequest lacks.
        self._hook_answered_ids = set()
        self._idle_published_epoch = None  # epoch whose Stop already published idle (idempotency)
        # precedence & lost-hook reconciliation (Task 7): process-exit > hook > bounded screen.
        self._last_hook_at = None          # clock of the most recent hook (lost-hook deadlines)
        self._hook_startup_at = None       # task-delivery clock for a hook-capable session (grace)
        # lost-Stop screen reconciliation state (only consulted while hook_mode == "active", busy):
        self._hook_progress_fp = None      # last screen meaning seen since the last hook (baseline)
        self._hook_progress_at = None      # clock at which that meaning last ADVANCED (a moving
        #                                    spinner is not progress); seeded to the last hook time
        self._reconcile_fp = None          # the free-text prompt region being watched for stability
        self._reconcile_since = None       # clock at which that free-text prompt first appeared
        # state snapshot
        self._state = EngineState()

    @property
    def state(self):
        return self._state

    @property
    def hook_mode(self):
        """"unknown" until the first hook arrives, then "active" (Claude reports its own lifecycle).
        "unavailable" (hookless agent / hook transport failure) is set by the precedence layer (Task 7)."""
        return self._hook_mode

    @property
    def turn_epoch(self):
        """Monotonic turn counter — bumped on every UserPromptSubmit. A decision is keyed to its epoch
        so a straggler hook from a closed turn cannot re-open it, and each turn gets a distinct idle."""
        return self._turn_epoch

    # ---- hook capability (Task 7): a hook-capable session arms the startup grace on task-delivery ----
    def expect_hooks(self, now):
        """Declare this session hook-capable and arm the startup grace at task-delivery (spec §6).

        Called by Session for a `hook_capable` driver when a task is delivered. While hook_mode is
        "unknown" within `hook_startup_grace` of this point the screen fallback stays conservative
        (it will not declare a screen-derived free-text idle — a hook may still be arriving); if the
        grace expires with no hook, `tick` flips hook_mode to "unavailable" and the screen path runs
        exactly as today. A screen-only session never calls this, so its screen path is untouched.
        """
        if self._hook_mode == "unknown":
            self._hook_startup_at = now

    # ---- hook path (Task 6): authoritative ground-truth state from the agent's own hooks ----
    def on_hook(self, hobs, now):
        """Fold one normalized hook observation into the belief state (spec §hook path).

        The first hook proves the agent reports its own lifecycle, so `hook_mode` becomes "active".
        `UserPromptSubmit` opens a new turn epoch (busy); `Stop`/`StopFailure` closes it, withdrawing
        any pending prompt and publishing the new non-respondable `idle` decision (the session stays
        alive). A permission/modal hook publishes a respondable `waiting_for_user`; its resolution
        (PostToolUse[AskUserQuestion]) withdraws it back to busy. A plain tool-lifecycle hook after a
        close with no intervening open is a straggler and is ignored. Returns the action list Session
        applies; the screen `tick` path is untouched here (precedence is Task 7).
        """
        first_hook = self._hook_mode != "active"
        self._hook_mode = "active"
        actions = []
        # The FIRST hook takes over from the screen fallback (spec §6): withdraw any stale screen-
        # published respondable decision (e.g. a lingering waiting_for_user) so it does not survive
        # into the hook-owned snapshot. _withdraw clears the screen path's _published_* bookkeeping.
        if first_hook and self._published_key is not None:
            self._withdraw(actions, "superseded", now)
        # Any hook is a fresh ground-truth signal: record it and re-baseline the lost-hook
        # reconciliation clocks (a new hook means the agent is alive and communicating).
        self._last_hook_at = now
        self._hook_progress_fp = None
        self._hook_progress_at = None
        self._reconcile_fp = None
        self._reconcile_since = None

        # UserPromptSubmit: a fresh user turn -> new epoch, busy, nothing published.
        if hobs.opens_turn:
            self._turn_epoch += 1
            self._turn_open = True
            self._hook_pending_key = None
            self._hook_pending_kind = None
            self._hook_answered_ids.clear()         # nelix-f7y: a new epoch retires the answered-gate tombstones
            self._state.control_state = "busy"
            self._state.phase = "busy"
            return actions

        # Stop/StopFailure: the turn ended -> idle. Idempotent within an epoch (a duplicate Stop, or a
        # StopFailure trailing a Stop, is one idle, not two).
        if hobs.closes_turn:
            if self._idle_published_epoch == self._turn_epoch:
                return actions
            self._turn_open = False
            self._idle_published_epoch = self._turn_epoch
            self._hook_answered_ids.clear()                      # nelix-f7y: epoch closed -> retire the tombstones
            if self._hook_pending_key is not None:               # a pending prompt is moot once idle
                actions.append(Withdraw(decision_key=self._hook_pending_key, reason="superseded"))
                self._hook_pending_key = None
                self._hook_pending_kind = None
            self._state.control_state = "idle"
            self._state.phase = "idle"
            actions.append(Publish(kind="idle", respondable=False,
                                   decision_key=f"idle:{self._turn_epoch}",
                                   payload={"interrupted": hobs.interrupted}))
            return actions

        # PostToolUse[AskUserQuestion]: the modal was answered -> withdraw the pending prompt, resume.
        # A late/out-of-order clears_pending arriving AFTER Stop closed this turn is a straggler: it
        # must NOT drag a closed (idle) turn back to busy (spec §5 guards). Ignore it unless a newer
        # UserPromptSubmit reopened the turn. (A first-hook clears with no close is NOT a straggler.)
        if hobs.clears_pending:
            if self._turn_closed():
                return actions
            if self._hook_pending_key is not None:
                actions.append(Withdraw(decision_key=self._hook_pending_key, reason="prompt_left"))
                self._hook_pending_key = None
                self._hook_pending_kind = None
            self._turn_open = True
            self._state.control_state = "busy"
            self._state.phase = "busy"
            return actions

        # PermissionRequest / PreToolUse[AskUserQuestion] / permission Notification: blocked on the user.
        # Same closed-turn guard as clears_pending/plain-working (spec §5): a late waiting_for_user
        # arriving AFTER Stop closed this turn is a straggler and must NOT resurrect it into
        # awaiting_user — ignore it unless a newer UserPromptSubmit reopened the turn.
        if hobs.kind == "waiting_for_user":
            if self._turn_closed():
                return actions
            # The pause is keyed by a per-gate FINGERPRINT of tool_name + tool_input (hooks.gate_fp):
            # stable across the two hooks a single physical AskUserQuestion modal emits —
            # PreToolUse[AskUserQuestion] (-> modal_choice) AND PermissionRequest (-> permission_choice)
            # both carry the SAME {questions:[...]} (nelix-32f collision) — and DIFFERENT for a genuinely
            # new gate (different tool_input). The PermissionRequest hook carries NO tool_use_id, so the
            # body fingerprint is the only per-gate identity. When a hook carries no tool_input (a legacy
            # permission Notification), fall back to the epoch key (see the _hook_answered_ids residuals).
            decision_key = (f"hook_gate:{hobs.gate_fp}" if hobs.gate_fp
                            else f"hook_pause:{self._turn_epoch}")
            effective_kind = self._preferred_prompt_kind(self._hook_pending_kind, hobs.prompt_kind)
            if self._hook_pending_key == decision_key:
                # this gate's pause is already published. Only a kind UPGRADE re-emits — modal_choice
                # arriving after permission_choice (the collision's two hooks, possibly reordered) — and
                # as a re-emit (SAME decision_key) so the decision_id stays stable (a held answer binds).
                if effective_kind != self._hook_pending_kind:
                    self._hook_pending_kind = effective_kind
                    actions.append(self._hook_pause_publish(decision_key, effective_kind))
                self._turn_open = True
                self._state.control_state = "awaiting_user"
                self._state.phase = "pause_candidate"
                return actions
            if decision_key in self._hook_answered_ids:
                # nelix-f7y: this gate was already ANSWERED. A waiting_for_user for the SAME key is a
                # late duplicate / straggler of that answered gate (reordered/duplicated hook delivery),
                # NOT a fresh gate — suppress the re-publish so no PHANTOM respondable decision is minted
                # (which the orchestrator would answer, leaking a stray keystroke into the resumed turn).
                # A genuinely new gate has a DIFFERENT id, so it is NOT in this set and publishes below —
                # no dependence on hook ordering or on a PostToolUse ever arriving (nelix-f7y wave 2).
                # The agent already resumed on the answer, so the true state is busy, not awaiting_user.
                self._turn_open = True
                self._state.control_state = "busy"
                self._state.phase = "busy"
                return actions
            self._hook_pending_key = decision_key
            self._hook_pending_kind = effective_kind
            self._turn_open = True
            self._state.control_state = "awaiting_user"
            self._state.phase = "pause_candidate"
            actions.append(self._hook_pause_publish(decision_key, effective_kind))
            return actions

        # A plain working hook (Pre/PostToolUse, PostToolUseFailure, ...) is meaningful only inside an
        # open turn. After a Stop with no new UserPromptSubmit it is a late straggler -> ignored (the
        # session must stay idle, not be dragged back to busy by trailing tool events).
        if self._turn_open:
            # nelix-f7y: a fresh PreToolUse carrying a gate fingerprint is a NEW-INVOCATION signal (only
            # PreToolUse carries gate_fp here). If that fingerprint is tombstoned, the agent is invoking
            # the SAME gate AGAIN (an allow-once approval + a re-run of the identical command), so LIFT
            # the tombstone -> the gate's next PermissionRequest publishes a fresh answerable decision
            # instead of being swallowed as a straggler. A gate's OWN first PreToolUse precedes its first
            # PermissionRequest, so its fingerprint is not yet answered -> discard is a harmless no-op.
            if hobs.gate_fp is not None:
                self._hook_answered_ids.discard(f"hook_gate:{hobs.gate_fp}")
            self._state.control_state = "busy"
            self._state.phase = "busy"
        return actions

    @staticmethod
    def _preferred_prompt_kind(a, b):
        # The more specific reading of a pause wins: modal_choice (a numbered AskUserQuestion menu)
        # over permission_choice (a generic allow/deny). Used to merge the two hooks one physical
        # AskUserQuestion modal emits into one epoch-scoped decision (nelix-32f).
        if a == "modal_choice" or b == "modal_choice":
            return "modal_choice"
        return b if a is None else a

    def _hook_pause_publish(self, decision_key, prompt_kind):
        hint = "needs_permission" if prompt_kind == "permission_choice" else None
        return Publish(kind="waiting_for_user", respondable=True,
                       decision_key=decision_key,
                       payload={"prompt_kind": prompt_kind, "hint": hint})

    def _turn_closed(self):
        """A turn is CLOSED once a Stop/idle published for the CURRENT epoch and no newer
        UserPromptSubmit reopened it: `_idle_published_epoch == _turn_epoch` (Stop sets them equal;
        a fresh UserPromptSubmit bumps `_turn_epoch` past `_idle_published_epoch`). A never-opened
        turn (fresh engine, epoch 0, nothing published) is NOT closed — so a waiting_for_user that
        arrives as the very first hook still surfaces (it is not a straggler)."""
        return self._idle_published_epoch == self._turn_epoch

    # ---- diagnostic notes (nelix-jwv): pure, off the action list; Session drains + logs ----
    def _note(self, event, **fields):
        self._notes.append(Note(event, fields))

    def drain_notes(self):
        notes, self._notes = self._notes, []
        return notes

    def _suppressed(self, reason):
        # Edge-triggered: emit one note when the suppression reason CHANGES (a stall that suppresses
        # for many ticks under the same reason logs exactly once until the reason changes or we act).
        if reason != self._suppress_reason:
            self._suppress_reason = reason
            self._note("belief_suppressed", reason=reason)

    # ---- submit edge (post-submit suppression entry, spec §7.1) ----
    def on_submit(self, text):
        # The agent just received our text. Forget any published decision (it was answered / will be
        # superseded), reset the idle candidate, and ENTER post_submit_ttft: while active, an
        # ambiguous free_text idle is suppressed (the echoed submission is not a fresh prompt, F1).
        now = self._clock.now()
        self._published_key = None
        self._published_kind = None
        # nelix-f7y: the answer resolves the pending hook pause. Free the pending slot AND tombstone the
        # answered gate's key: a late duplicate / straggler of THIS gate (reorderable hook delivery)
        # then reuses the same per-gate key and is suppressed in on_hook (no PHANTOM respondable
        # decision), while a genuinely FRESH subsequent gate has a DIFFERENT key and publishes normally.
        # Because the key is the per-gate fingerprint (not the epoch), the tombstone can never suppress
        # a real new gate — so there is no hook-ordering / PostToolUse dependence and no terminal hang.
        if self._hook_pending_key is not None:
            self._hook_answered_ids.add(self._hook_pending_key)
        self._hook_pending_key = None
        self._hook_pending_kind = None
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
        self._suppress_reason = None             # a fresh turn: re-arm the suppression edge
        self._note("post_submit_armed", grace=self._cfg.post_submit_grace)

    # ---- main entry ----
    def tick(self, obs, ctx):
        now = self._clock.now()
        actions = []
        self._track_semantic(obs, now)
        self._track_heartbeat(obs, now)

        # (1) Process exit / crash ALWAYS wins (spec §6, highest precedence): a dead child, or a
        # crash/exit screen, is terminal regardless of the last hook.
        if not ctx.child_alive or obs.prompt_kind in ("crash", "exit"):
            self._state.control_state = "terminal"
            self._state.phase = "terminal"
            actions.append(Finalize())
            return actions

        # A hook-capable session that has waited out the startup grace with no hook falls back to
        # screen-driven "unavailable" — from here it behaves exactly as a hookless agent (today).
        self._maybe_expire_startup_grace(now)

        # (2) hook_mode == "active": hooks are ground truth. tick() does NOT publish a screen-derived
        # waiting_for_user/idle; it only runs the watchdog + the bounded lost-hook reconciliation.
        if self._hook_mode == "active":
            return self._tick_hook_active(obs, now, actions)

        # (3) hook_mode "unknown"/"unavailable": the screen path (today). During the "unknown"
        # startup grace _on_prompt stays conservative about a screen-derived free-text idle.
        self._update_post_submit(obs, now)
        if obs.prompt_kind in _PROMPT_KINDS:
            self._on_prompt(obs, now, actions)
        else:
            self._on_busy(obs, now, actions)

        self._refresh_state(obs, now)
        return actions

    # ---- precedence & lost-hook reconciliation (Task 7): process > hook > bounded screen ----
    def _maybe_expire_startup_grace(self, now):
        # A hook-capable session (expect_hooks armed _hook_startup_at) that never received a hook
        # transitions "unknown" -> "unavailable" once the startup grace elapses (spec §6): the screen
        # then drives the session for its life, exactly as a hookless agent does today.
        if (self._hook_startup_at is not None and self._hook_mode == "unknown"
                and now - self._hook_startup_at >= self._cfg.hook_startup_grace):
            self._hook_mode = "unavailable"

    def _in_hook_startup_grace(self, now):
        # True only for a hook-capable session still inside the startup grace with no hook yet. For a
        # screen-only session (never armed) this is always False -> the screen path is untouched.
        return (self._hook_startup_at is not None and self._hook_mode == "unknown"
                and now - self._hook_startup_at < self._cfg.hook_startup_grace)

    def _tick_hook_active(self, obs, now, actions):
        # Hooks own control_state (busy/awaiting_user/idle set by on_hook). The screen may ONLY
        # (a) reconcile a lost Stop and (b) escalate a lost-hook stall; it NEVER publishes a
        # screen-derived waiting_for_user. Reconciliation applies only while hooks say busy (or while
        # a prior lost-hook intervention is still active — so the ladder nags and can revert to busy).
        if self._state.control_state not in ("busy", "intervention_required"):
            self._refresh_hook_fields(obs, now)
            return actions

        if obs.prompt_kind == "free_text":
            # A stable free-text prompt after the last hook = the agent finished but its Stop was
            # lost -> reconcile to a low-confidence idle (reconciled=True), never waiting_for_user.
            self._reconcile_lost_stop_idle(obs, now, actions)
            if actions:
                return actions
        else:
            self._reconcile_fp = None
            self._reconcile_since = None
            self._track_hook_progress(obs, now)
            if self._lost_stop_stuck(now):
                # Return before the watchdog: it resets _intervention_active off SCREEN quiet (which
                # is 0 on a cold frame), which would clobber the lost-Stop escalation we just made.
                self._escalate_lost_stop(obs, now, actions)
                return actions
        # the frozen-meaning watchdog ladder sits over hook mode too (spec §6); its escalation state
        # then owns control_state (real semantic progress reverts it to busy via _track_semantic).
        self._watchdog(obs, now, actions)
        self._state.control_state = "intervention_required" if self._intervention_active else "busy"
        self._refresh_hook_fields(obs, now)
        return actions

    def _track_hook_progress(self, obs, now):
        # "screen progress" = the screen's MEANING advancing after the last hook (a moving spinner is
        # not progress). The first screen frame after a hook is the baseline whose clock is the hook
        # time, so a frozen screen reads as "quiet since the hook", not spuriously fresh.
        if self._hook_progress_fp is None:
            self._hook_progress_fp = obs.semantic_fp
            self._hook_progress_at = self._last_hook_at if self._last_hook_at is not None else now
        elif obs.semantic_fp != self._hook_progress_fp:
            self._hook_progress_fp = obs.semantic_fp
            self._hook_progress_at = now

    def _lost_stop_stuck(self, now):
        # busy per hooks, but no new hook AND no screen progress for lost_stop_after -> stuck agent.
        hook_silent = (self._last_hook_at is not None
                       and now - self._last_hook_at >= self._cfg.lost_stop_after)
        no_progress = (self._hook_progress_at is not None
                       and now - self._hook_progress_at >= self._cfg.lost_stop_after)
        return hook_silent and no_progress

    def _reconcile_lost_stop_idle(self, obs, now, actions):
        key = obs.prompt_fp or obs.semantic_fp
        if self._reconcile_fp != key:
            self._reconcile_fp = key
            self._reconcile_since = now
        stable = (now - self._reconcile_since) >= self._cfg.hook_turn_grace
        since_hook = (self._last_hook_at is None
                      or (now - self._last_hook_at) >= self._cfg.hook_turn_grace)
        if not (stable and since_hook):
            return
        # supersede the busy hook state with a reconciled idle; mirror on_hook's idle bookkeeping so
        # a straggler Stop for this epoch stays idempotent and a follow-up re-opens a fresh turn.
        self._turn_open = False
        self._idle_published_epoch = self._turn_epoch
        self._reconcile_fp = None
        self._reconcile_since = None
        self._state.control_state = "idle"
        self._state.phase = "idle"
        actions.append(Publish(kind="idle", respondable=False,
                               decision_key=f"idle:{self._turn_epoch}",
                               payload={"interrupted": False, "reconciled": True}))

    def _escalate_lost_stop(self, obs, now, actions):
        # Reuse the watchdog nag ladder (shared counter/throttle): a NON-respondable advisory that
        # re-fires each budget while still stuck. The daemon never acts on the agent; the
        # orchestrator drives recovery off this wake.
        self._intervention_active = True
        budget = self._budget(self._liveness(obs, now), obs.busy_reason)
        if self._next_nag_at is None or now >= self._next_nag_at:
            self._escalation_count += 1
            self._next_nag_at = now + budget
            self._state.control_state = "intervention_required"
            self._state.phase = "lost_stop"
            actions.append(Publish(
                kind="intervention_required", respondable=False,
                decision_key=f"intervention:{self._escalation_count}",
                payload={"escalation_count": self._escalation_count,
                         "busy_reason": obs.busy_reason,
                         "liveness": self._liveness(obs, now),
                         "hint": "lost_stop"}))

    def _refresh_hook_fields(self, obs, now):
        # Keep the diagnostic fields fresh WITHOUT overwriting the hook-owned control_state.
        self._state.quiet_elapsed = (0.0 if self._semantic_change_ts is None
                                     else now - self._semantic_change_ts)
        self._state.liveness = self._liveness(obs, now)
        self._state.busy_reason = obs.busy_reason
        self._state.escalation_count = self._escalation_count

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
            self._note("post_submit_cleared", reason="grace_expired")
            return
        if obs.prompt_kind == "none" and self._liveness(obs, now) == "live":   # (a) busy + live
            self._post_submit_active = False
            self._note("post_submit_cleared", reason="busy_live")
            return
        if (not obs.submitted_echo_present
                and obs.content_fp != self._post_submit_content_fp):           # (b) real output
            self._post_submit_active = False
            self._note("post_submit_cleared", reason="real_output")
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
        self._suppress_reason = None             # turn moved: re-arm the suppression edge
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
        # hook startup grace (Task 7, spec §6): a hook-capable session whose first hook has not yet
        # arrived stays conservative — do NOT declare a screen-derived free-text idle while a hook
        # may still be coming. Modal/permission (blocking, high-confidence) above still publish.
        if self._in_hook_startup_grace(now):
            self._suppressed("hook_startup_grace")
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
                self._suppressed("submitted_echo_present")
                return
            self._escalate_stuck_input(obs, now, actions)
            return
        self._echo_since = None                       # echo gone: the submit landed, clear the clock
        if self._post_submit_active and now < self._post_submit_grace_until:
            self._suppressed("post_submit_grace")
            return
        # anti-flap: do not re-mint the SAME semantic_fp within the cooldown after withdrawing it.
        if (obs.semantic_fp == self._last_withdrawn_fp
                and self._withdrawn_cooldown_until is not None
                and now < self._withdrawn_cooldown_until):
            self._suppressed("anti_flap")
            return
        settled = (now - self._candidate_since) >= self._cfg.idle_confirm_window
        if not settled:
            self._suppressed("not_settled")
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
        self._suppress_reason = None              # we published: re-arm the suppression edge
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
