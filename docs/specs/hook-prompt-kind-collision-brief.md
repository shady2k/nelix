# Brief — nelix-32f (AskUserQuestion hook prompt_kind collision)

**Read first:** `docs/specs/hook-prompt-kind-collision.md` (full diagnosis + AC1–AC5 + evidence).
**Bead:** `bd show nelix-32f`.

## Your task (TDD, real-capture, no fabricated frames)

Fix the production-breaking bug where a single AskUserQuestion modal emits BOTH
`PreToolUse[AskUserQuestion]` (→ modal_choice) AND `PermissionRequest`
(→ permission_choice), and nelix lets the second hook SUPERSEDE the first
(`daemon/belief.py:230` keys the pending slot by `prompt_kind`), so the orchestrator
can never answer the modal.

### Repro fixtures (already staged — committed on main)
- `tests/golden/claude/_regression/s-9610d25c-askuserquestion-collision.{raw,capture}` — real PTY of the 6-option modal.
- `tests/golden/claude/_regression/s-9610d25c-askuserquestion-collision.hooks.jsonl` — ground-truth hook sequence, replayable via `tests/_session_replay.py::replay_hooks`.

### Do this, in order
1. **RED unit test** in `tests/test_belief_hooks.py`: `on_hook` sequence
   `[UserPromptSubmit, PreToolUse[AskUserQuestion], PermissionRequest]` → assert exactly ONE `Publish`, stable `decision_id` across the third hook, final `prompt_kind == modal_choice`. Confirm it fails on current code.
2. **RED replay test** in `tests/test_session_replay.py`: drive the real Session with the staged hook jsonl + the modal screen frame → one respondable decision; `respond(option_id)` succeeds.
3. **GREEN**: minimal reconciliation in `daemon/belief.py` — within a turn-epoch, a second `waiting_for_user` hook with a different `prompt_kind` is an IN-PLACE update (prefer `modal_choice` over `permission_choice` when both point at the same epoch), NOT a supersede. Do not weaken the straggler/Stop/`clears_pending` guards.
4. Confirm the design question in the spec's "Design tension" section (does `AskUserQuestion` itself fire `PermissionRequest` here? scan the daemon log around 2026-07-04T20:57:20 for a concurrent Bash tool). The merge semantics must hold either way, but note what you find.

### Constraints (nelix bar)
- Real-capture only; never fabricate frames. Reuse the staged fixtures.
- `make test` MUST be green on the daemon's Python **3.11** (the repo `.venv` is 3.11; the daemon runs 3.11). Do not assume 3.14.
- Do not edit code on `main`; commit on your worktree branch.
- Don't touch the unrelated follow-ups nelix-4ei (redaction) / nelix-4q8 (capture dedup).

### Done = AC1–AC5 met, `make test` green on 3.11, then report.
