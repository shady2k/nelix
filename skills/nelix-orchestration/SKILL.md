---
name: nelix-orchestration
description: How to delegate a coding/dev task to an agentic CLI executor via the nelix_* tools — drive it and relay its decisions to the user. Use whenever a nelix session is active.
---

# Orchestrating a CLI executor with Nelix

Nelix delegates work to an **agentic CLI executor** — an autonomous coding agent (e.g. OpenCode)
with its own plan, opinions, and tools. It runs on its own and pauses only at **decision points**
(permission prompts, choices). You are the orchestrator; the CLI is the executor; the **user decides**.
Your job is to relay each decision to the user and feed the answer back — not to decide yourself.

## Loop
1. `nelix_start(executor, task)` → returns `session_id`. The executor now runs on its own and a
   background waiter is armed for you. **End your turn here.** Do not call any nelix tool again now.
   Between now and the wake-up the executor uses **none of your tokens**.
2. **You will be woken** at the next decision point or at completion. *When woken* — and only then —
   call `nelix_status(session_id)` **exactly once** to read the current `state` and any `decision`.
   (Once per wake-up, to absorb a missed/duplicated wake-up — never in a loop within a turn.)
3. If `nelix_status` returns a `decision` (state `idle_prompt` or `permission_prompt`), the executor is
   **paused awaiting an answer**. Relay `decision.text` to the user verbatim (label by executor/session
   if several are active). The **user decides** — don't answer it yourself.
   - `decision.hint == "needs_permission"` → a permission menu (the answer is usually a number, e.g. `1`).
     Otherwise it's a free-text question.
   - `decision.text` is the tail of the turn (capped). To read the **full question or an earlier turn**
     the wake-up summarized, call `nelix_dialog(session_id)` (latest turn) or
     `nelix_dialog(session_id, turn=N, offset=M)` to page.
   - `decision.hung == true` → the executor made no progress for a long time and nelix nudged it with
     ESC; it may be wedged. Relay that and let the user decide (answer, or `nelix_stop`).
4. `nelix_respond(session_id, decision.event_id, answer)` with the user's answer; pass the last-seen
   event `seq` as `after_seq` so the next wake fires on a NEW decision. Then **end your turn again** —
   you'll be woken on the next decision.
5. When `nelix_status` reports `state` `exited`, the executor's process finished — report the result.
   On `crashed`, report the failure.
6. `nelix_stop(session_id)` to abort.

## Rules
- **After `nelix_start` / `nelix_respond`, if the session is still running, STOP — end your turn.**
  Do NOT poll `nelix_status` (or any nelix tool) in a loop waiting for it to finish: that burns tokens
  on every call for nothing. The whole point of nelix is that you sleep between decisions and the
  wake-up brings you back. Call `nelix_status` only once per wake-up to reconcile.
- The executor name comes from the nelix config; pass it to `nelix_start`.
- Decisions are the user's call — surface them, don't answer them yourself.
- `done` is the executor's **process exiting** (`state: exited`), not a mere idle prompt — a returned-to-
  input executor that's still alive is a decision point (`idle_prompt`), so relay it, don't assume done.
- Use `nelix_dialog` to read transcript, never to poll; it reads durable history, not live progress.
