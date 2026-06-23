---
name: nelix-orchestration
description: How to delegate a coding/dev task to an agentic CLI executor via the nelix_* tools — drive it and relay its decisions to the user. Use whenever a nelix session is active.
---

# Orchestrating a CLI executor with Nelix

Nelix delegates work to an **agentic CLI executor** — an autonomous coding agent (e.g. Claude Code)
with its own plan, opinions, and tools. It runs on its own and pauses only at **decision points**
(permission prompts, choices). You are the orchestrator; the CLI is the executor; the **user decides**.
Your job is to relay each decision to the user and feed the answer back — not to decide yourself.

## Loop
1. `nelix_start(executor, task)` → returns `session_id`. The executor runs autonomously; you sleep.
2. You are woken at the next decision (or completion). On **every turn while a session is active**, call
   `nelix_status()` to read state + any pending decision — never trust a single wake-up (it can be
   missed or duplicated).
3. Relay the pending decision to the user verbatim. If several sessions are active, label it by
   executor/session.
4. `nelix_respond(session_id, event_id, answer)` with the user's answer. Pass the last-seen event `seq`
   as `after_seq` so the next wake-up fires on a NEW decision.
5. Repeat until the session reports `done` (report the result) or `crashed`.
6. `nelix_stop(session_id)` to abort.

## Rules
- The executor name comes from the nelix config; pass it to `nelix_start`.
- Decisions are the user's call — surface them, don't answer them yourself.
- Between decisions the executor uses none of your tokens; don't tight-loop — the wake-up brings you back.
