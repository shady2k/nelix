# AskUserQuestion modal misclassified as `permission_choice` (hook prompt_kind collision)

Production-breaking: the orchestrator cannot answer an AskUserQuestion modal in the
`claude` executor. Found live in session `s-9610d25c` (2026-07-04).

## Bug

A single on-screen AskUserQuestion modal emits BOTH `PreToolUse[AskUserQuestion]`
(→ `modal_choice`) AND `PermissionRequest` (→ `permission_choice`) hooks. Because
`BeliefEngine.on_hook` keys the pending prompt by `prompt_kind:turn_epoch`
(`daemon/belief.py:230`), the two hooks produce two DIFFERENT decision keys; the
second supersedes the first (`belief.py:231-241` only no-ops on an EXACT key match).
When `PermissionRequest` arrives second, the modal is reclassified `permission_choice`,
the stable `modal_choice` decision is withdrawn, and `respond()` then rejects the
orchestrator's option answer (`session.py:1592-1595`). The orchestrator can never answer.

## Evidence — `s-9610d25c`, 2026-07-04 20:57:20

daemon log `daemon-20260704-201855-44106.log`:

| line | event |
|------|-------|
| 115  | `decision_published dec-cac4e8f5 prompt_kind=modal_choice` (PreToolUse[AskUserQuestion]) |
| 116  | `hook_applied raw_event=PreToolUse kind=waiting_for_user` |
| 118  | `decision_superseded dec-cac4e8f5 superseded_by dec-0903ad69` |
| 119  | `decision_published dec-0903ad69 prompt_kind=permission_choice` |
| 120  | `hook_applied raw_event=PermissionRequest kind=waiting_for_user` |

Then `errors.log`: `nelix_respond missing_decision_id` (20:58:21) → `invalid_option`
×3 (20:58:30/34/40); decision `stopped` 21:00:28. **Modal never answered.**

## Root cause (code)

- `daemon/hooks.py:55-58` — `PermissionRequest` → `prompt_kind="permission_choice"`
- `daemon/hooks.py:61-65` — `PreToolUse[AskUserQuestion]` → `prompt_kind="modal_choice"`
- `daemon/belief.py:230` — `decision_key = f"{hobs.prompt_kind}:{self._turn_epoch}"`
- `daemon/belief.py:231-232` — only an EXACT-key match is a no-op; a different
  `prompt_kind` → new `Publish` → supersede of the prior decision.
- `daemon/session.py:1592-1595` — `respond()` rejects any answer not in option ids
  for BOTH `modal_choice` and `permission_choice`.

## Acceptance criteria (invariant; implementation TBD — worker + Codex)

- **AC1** Within one turn-epoch, a single physical modal that emits both
  `PreToolUse[AskUserQuestion]` and `PermissionRequest` results in EXACTLY ONE
  respondable `waiting_for_user` decision with a STABLE `decision_id` — the second
  hook MUST NOT supersede/withdraw the first.
- **AC2** That decision's `prompt_kind` reflects the actual on-screen modal:
  `modal_choice` when ≥3 selectable options are present (the 6-option "Next step"
  modal). A genuine 2-option Bash permission in an epoch with NO `AskUserQuestion`
  stays `permission_choice`.
- **AC3** `respond()` against the single stable decision with a valid option id
  succeeds (no `missing_decision_id`, no `invalid_option`).
- **AC4** No regression: the existing straggler / Stop / `clears_pending` semantics
  in `tests/test_belief_hooks.py` stay green; a real Bash permission (no
  `AskUserQuestion` in the epoch) still publishes `permission_choice` and is answerable.
- **AC5** Real-capture replay of the staged fixtures publishes one `modal_choice`
  decision answerable end-to-end.

## Repro fixtures (staged under `tests/golden/claude/_regression/`)

- `s-9610d25c-askuserquestion-collision.raw` / `.capture` — real PTY capture of the
  6-option "Next step" AskUserQuestion modal.
- `s-9610d25c-askuserquestion-collision.hooks.jsonl` — ground-truth hook sequence
  from the daemon log (`UserPromptSubmit` → `PreToolUse[AskUserQuestion]` →
  `PermissionRequest`), replayable via `tests/_session_replay.py::replay_hooks`.

## Test plan (TDD)

1. **RED unit** in `tests/test_belief_hooks.py`: `on_hook` sequence
   `[UserPromptSubmit, PreToolUse[AskUserQuestion], PermissionRequest]` → assert
   exactly ONE `Publish`, `decision_id` stable across the third hook, `prompt_kind`
   ends `modal_choice`.
2. **RED replay** in `tests/test_session_replay.py`: drive the real Session with the
   staged hook jsonl + the modal screen frame → assert one respondable decision and
   that `respond(option_id)` succeeds.
3. **GREEN**: minimal reconciliation in `belief.py` — within an epoch, a second
   `waiting_for_user` hook with a different `prompt_kind` is an IN-PLACE update
   (preferring the more specific `modal_choice`), NOT a supersede. Validate via the
   RED tests + Codex whole-branch review.
4. `make test` on the daemon's Python **3.11** before merge.

## Design tension (for the worker + Codex)

The hook path is intentionally authoritative over the screen tick (precedence by
design). The fix must keep "one pending prompt per epoch" without letting one
physical modal's two hook emissions split into two decisions. Confirm whether
`PermissionRequest` is fired BY `AskUserQuestion` in this claude build (vs a
concurrent real Bash approval) — scan the daemon log around 20:57:20 for any Bash
tool pending; if `AskUserQuestion` itself fires it, the merge semantics of AC1/AC2
are the correct fix.
