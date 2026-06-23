# Nelix — Business Requirements

| | |
|---|---|
| **Project** | Nelix |
| **Document** | Business Requirements (BRD) |
| **Status** | Draft for review |
| **Owner** | Project maintainer (single operator) |
| **Date** | 2026-06-23 |
| **Companion** | [`product-specification.md`](./product-specification.md) — the technical "how" |

> This document captures the **why** and the **what** at a business level. The orchestrator host is
> **Hermes** (this is a Hermes plugin); everything else is vendor-neutral — no specific executor CLI,
> messaging platform, or runtime is named.

## 1. Executive Summary

**Nelix** is a **Hermes** plugin that lets Hermes (the user's Orchestrator Agent — an LLM-driven
assistant) drive an arbitrary interactive **agentic CLI** as a task executor. The user delegates a
development task to Hermes through their normal messaging channel; Hermes launches the CLI, lets it
run autonomously, and is woken only at **decision points** (questions, permission requests, completion,
errors) to relay them to the user and inject answers back. The orchestration intelligence stays in
Hermes; the CLI is a **pluggable executor** reached through **one universal mechanism** — the terminal
itself (a PTY + terminal emulator) — not any vendor-specific integration surface.

## 2. Problem & Motivation

- The user runs more than one agentic coding CLI and wants a **single entry point** for all dev tasks
  instead of switching tools and terminals.
- He wants to use an executor CLI's **own authenticated subscription/session** as the execution
  engine, with **no metered per-token API cost billed through Nelix** for that work.
- He already runs **Hermes** as his personal assistant and wants it to stay the orchestrator in the
  loop, rather than handing control to a tool-specific daemon.
- **Vendor lock-in** is a concern: the mechanism must not bind to any single executor CLI's
  proprietary surface.

## 3. Vision

One conversation from which the user can spawn, supervise, answer, and complete development tasks
executed by whichever agentic CLI is best for the job — with Hermes mediating. Model-agnostic,
subscription-based, no executor lock-in.

## 4. Goals & Objectives

| ID | Goal |
|----|------|
| G1 | **Single entry point** — all dev tasks initiated and supervised from one conversation in Hermes |
| G2 | **Subscription execution** — use the executor CLI's own authenticated session; no metered API token cost billed through Nelix for the executor's work |
| G3 | **Hermes is the sole mediator** — the executor has no channel to the user; the user sees only what Hermes relays (no standalone tool→channel integration) |
| G4 | **Universality / no lock-in** — one orchestration mechanism drives any CLI for which a driver exists |
| G5 | **Cost efficiency** — no Hermes-LLM token consumption from routine executor progress between decision points |
| G6 | **Responsiveness** — Hermes stays responsive to other messages during long CLI tasks |

## 5. Non-Goals (Out of Scope)

- **Not a standalone single-tool daemon** — not turning the CLI into its own service with direct access
  to the user's messaging channel (that would remove Hermes from the loop).
- **Not a model-provider integration** — not routing LLM calls through any vendor API.
- **Not bound to any single executor CLI's proprietary integration surface** (a structured
  non-interactive mode, a language-specific SDK, or executor-side hooks).
- **Not a sandbox.** Nelix does **not** restrict or sandbox the executor CLI — the CLI runs as the
  operator configured it, with its own rights and permission model. Nelix does not interfere with the
  CLI's access; OS-level isolation, if wanted, is the operator's responsibility.
- **No manual human takeover** of a running session (no attach-to-terminal requirement).
- **Not a general remote shell** — Nelix exposes no shell of its own; control is constrained to
  decision-point answers.

## 6. Stakeholders & Users

- **Primary user / owner:** the project maintainer — a **single, trusted operator** who runs Hermes
  themselves and interacts with it through a messaging channel.
- **Operator:** the same person (single-tenant, self-hosted).
- **Downstream:** additional executor CLIs as the catalogue of drivers grows.

## 7. Business Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| BR-1 | Delegate a development task to an agentic CLI from one conversation in Hermes | Must |
| BR-2 | Run the CLI autonomously with no Hermes-LLM cost between decision points | Must |
| BR-3 | Detect decision points (question / permission / completion / error) without an LLM | Must |
| BR-4 | Notify the user (via Hermes) at decision points with a concise summary | Must |
| BR-5 | Capture the user's answer and inject it back into the CLI | Must |
| BR-6 | Answer "how's it going?" on demand with current status | Must |
| BR-7 | Report completion with a result summary | Must |
| BR-8 | Keep Hermes responsive to unrelated messages during long tasks | Must |
| BR-9 | Run one active session in the MVP; concurrent sessions are **post-MVP** | Must (one session) |
| BR-10 | Allow stopping / cancelling a session | Should |
| BR-11 | A running session survives **Hermes** restarts without being lost | Must |
| BR-12 | Extend to a new CLI by adding a driver, with the core unchanged | Must |
| BR-13 | Secure Nelix's **own surfaces** — authenticated control plane, session authorization, answer-injection hygiene, secret redaction in outbound summaries, full audit — with **every decision point relayed to the user (no auto-approval in MVP)** | Must |
| BR-14 | Restart the executor CLI process within a running session — *fresh* or *resuming* its prior context (where the CLI supports resume) — and start a new session; recover from context-window exhaustion by starting a fresh session | Must |
| BR-15 | Tolerate transient executor/provider outages — recover automatically with bounded exponential backoff before escalating to the user | Must |

> **MVP scope note.** Auto-approval of "safe" prompts (a deny-by-default allowlist) is **post-MVP**;
> in the MVP **every** decision point is surfaced to the user. Concurrent sessions are post-MVP.

## 8. Success Criteria

The product is successful when:

1. A real multi-minute task (e.g. "refactor the auth module") runs **end-to-end from one conversation
   in Hermes**: launch → autonomous work → at least one answered decision point → completion report,
   with the user never leaving their messaging channel.
2. **No Nelix-owned API token billing** for the executor's work is verified (the CLI runs on its own
   subscription/session; the Hermes LLM is idle between decision points).
3. Hermes **answers an unrelated message** while a task is running.
4. A **second, different executor CLI** drives a task through the same core, proving universality.
5. In the MVP **every decision point is surfaced to the user — nothing is auto-approved** — and every
   control action appears in the audit log.

## 9. Constraints & Assumptions (business / environment)

- Single-tenant, self-hosted, operated by **one trusted person**.
- Hermes runs as a **persistent service** (gateway mode) that handles the user's messages
  asynchronously.
- The executor CLI and its **credentials live natively on the host**; the CLI runs with its own
  configured permissions (Nelix does not sandbox it — see Non-Goals).
- The integration is a **Hermes plugin**, with **no modifications to Hermes core**.
- The executor CLI's subscription / authentication terms must be respected.

## 10. Business Risks

| Risk | Mitigation |
|------|------------|
| **Wake-up reliability** — if the async wake-up is unreliable, the experience degrades to manual polling | Layered fallback notifications (spec §4). *De-risked 2026-06-23: the required Hermes capability is verified present and works in gateway mode; empirical reliability + terminal-tool lifetime are Phase-0 spikes.* |
| **Provider policy** — an executor CLI vendor could change subscription terms for programmatic use | Universality (G4) lets the executor be swapped without rewriting the core |
| **No CLI sandbox** — Nelix does not confine the executor; a bad instruction can touch anything the CLI can | Single trusted operator; always-ask in MVP; OS-level isolation is the operator's choice |
| **Control-plane exposure** — a messaging-channel→host bridge is a high-value target | Authenticated daemon, session authorization, audit log (spec §5) |
| **Maintenance** — each new CLI needs a driver | Accepted and budgeted as inherent (one driver per CLI) |

## 11. Glossary

- **Hermes** — the user's LLM-driven personal assistant (Orchestrator Agent); this product is a Hermes
  plugin.
- **Executor CLI** — the agentic coding CLI being orchestrated.
- **Driver** — the per-CLI integration (launch, classify, inject, resume, error taxonomy).
- **Decision point / event** — a moment the CLI needs input, or has finished / failed.
- **Host Daemon** — host-side process owning the PTY + emulator + drivers.
- **Messaging channel** — how the user communicates with Hermes.
- **Wake-up** — the mechanism that re-engages the Hermes LLM at a decision point.
