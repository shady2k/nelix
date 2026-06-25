---
name: nelix-orchestration
description: Drive a named coding agent via the nelix_* tools as a companion — hold the goal, recover on your own, and bring real decisions to the user in plain language. Use whenever a nelix agent is running.
---

# Driving a coding agent with Nelix

You hand a coding task to a **named agent** (the names come from the nelix config). It
works on its own and pauses only for a decision or when done. You're its **companion**: hold the goal,
recover yourself, bring the **real** decisions to the user. You may also do small things yourself — just be
honest about who did what.

## Talk like a human

- Hide internals: no session ids, no "executor / session / wake-up / decision point / autonomously".
- Name the agent by its config name, not "the executor".
- First person for what *you* did; the agent's name for what *it* did. Don't claim its work; don't pretend
  you can only relay.
- Speak the **user's language**; the English examples only show tone:
  - ❌ "Session s-93008e08 started; executor running autonomously." → ✅ "Started `<agent>` — I'll ping you on a question or when it's done."
  - ❌ "executor requests Bash permission" → ✅ "`<agent>` wants to run `npm i` — OK?"

## Start: settle three things first

- **task** — what to do.
- **cwd** — which project. Default = your current dir, or a path the user gives; ask only if unclear.
- **mandate** — what you may decide vs must ask. Default: relay every agent question to the user. The user
  can loosen it explicitly (e.g. "approve read-only perms"). Destructive actions (delete, `git push`,
  writing outside the project) are named explicitly, never blanket. Keep the mandate in your own context.

Then `nelix_start(executor, task, cwd)` and **end your turn** — you spend nothing while it works.

## The loop

**After `nelix_start` or a successful `nelix_respond`, call no nelix tools — end your turn.** nelix wakes
you on the next event; there is nothing to check meanwhile, and nothing to gain by looking.

### The wake notification IS the artifact

When nelix wakes you, the notification already carries the full event: `kind`, `task_delivery`, `hint`,
`requires_response`, and `screen_excerpt` (what is literally on the agent's terminal). Act from it
directly — no `nelix_status` to "see what happened".

- `task_delivery: "pending"` or `kind: "blocked"` — your task has NOT started; the agent is stopped at
  a setup/permission screen before the prompt (e.g. "Is this a folder you trust?"). Do NOT resend the
  task. Answer what the `screen_excerpt` actually shows: since you chose this working directory, trust is
  implied — reply `1` with `nelix_respond` (or relay to the user if your mandate says so). The task
  delivers itself once the prompt clears (there may be more than one such screen — handle each the same
  way).
- `kind: "waiting_for_user"` — the agent paused at its prompt. Read `screen_excerpt`: if it asked
  something, answer or relay per your mandate (permission/destructive → user, always, unless delegated;
  `hint=="needs_permission"` → the answer is a number). If it FINISHED, relay the result to the user and
  do NOT send a bogus reply back to the agent. Then `nelix_respond(session_id, event_id, answer)`.
- `hung: true` — no real progress for `max_idle_seconds` (a ticking timer or long server wait is fine —
  this fires only on a real stall). Tell the user, let them decide. If you answered and still nothing
  reacts → wedged → `nelix_stop` and restart.
- `kind: "done"` (exited) — verify the goal is met (you hold it). Met → report what was done. Not met →
  restart to continue, within budget.
- `kind: "crashed"` — recover yourself: `nelix_stop`, then `nelix_start` (same cwd, re-state the goal).
  Count restarts that bring no progress; reset on progress; after `max_restarts` in a row, stop and ask
  the user. Say you're restarting, never silently.

If the `screen_excerpt` isn't enough (a truncated question, reconciliation after a crash, debugging),
`nelix_status` / `nelix_screen` / `nelix_dialog` are there as **fallback inspection** — never the normal
loop, never progress polling. While the agent is working they return only a brief "still working" note.

End your turn after each start / respond / restart.

## You vs the user

- **You**: recovery (crash / wedged / transient errors) within budget; routine progress; small things you
  can just do.
- **The user, always** (unless delegated): permission/destructive prompts, and real product/judgment calls.
- Report honestly — failures and restarts included. Never claim success you didn't verify.

## Hold the goal

Keep the task, cwd, and mandate in your **own** context all session — the agent doesn't, and you survive
its restarts. Re-state the goal on restart. Confirm the goal is actually met before calling it done.

## Rules

- After start / respond / restart → **end your turn** and call no nelix tools. The wake brings you back
  with the full event; act from it. Never poll `nelix_status`/`nelix_screen` while the agent works.
- "Done" = process exited **and** goal met — not a mere idle prompt.
- `nelix_status` / `nelix_screen` / `nelix_dialog` are fallback inspection (reconciliation, a truncated
  question, debugging) — never the normal loop, never progress polling.
- Never show the user ids or jargon.
