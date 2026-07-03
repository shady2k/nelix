# Nelix — Product Specification

| | |
|---|---|
| **Project** | Nelix — Universal CLI Orchestrator Plugin for Hermes |
| **Document** | Product Specification |
| **Status** | Draft for review |
| **Owner** | Project maintainer (single operator) |
| **Date** | 2026-06-23 |
| **Companion** | [`business-requirements.md`](./business-requirements.md) — the business "why" |

> **Branding policy.** The orchestrator host is **Hermes** (this is a Hermes plugin) and is named
> throughout. Everything else is vendor-neutral: the orchestrated tool is "the **executor CLI**", the
> user channel is "the **messaging channel**", the isolated environment Hermes may run in is "the
> **orchestrator runtime**". No specific executor CLI, messaging platform, OS, or container runtime is
> named.

> **MVP policy.** (1) **Always ask the user** at every decision point — **no auto-approval** (§5, §8).
> (2) **One active session at a time** — concurrent sessions are post-MVP (§3.7). (3) **Nelix is not a
> sandbox** — it does not restrict the executor CLI; the CLI runs as configured, with its own rights
> and permission model (§5).

> **Revision note (2026-06-23):** Named **Nelix**, de-branded everything except Hermes, and folded an
> external design review. Major changes: the wake-up now uses an **opaque event envelope + canonical
> event fetch** (not raw stdout summaries) with an explicit **event lifecycle** (§4); the adapter is
> defined as a **per-tool driver** with a capability contract (§3.5); classifier hardened with
> **liveness signals + a `task_accepted` gate** (§3.6); lifecycle made safe (**idempotent
> resume**, **no silent resume→fresh downgrade**, autonomous backoff only when no side effects — §3.7);
> single active session (D1); **no-sandbox trust model** (D2); security reframed to Nelix's own
> surfaces (§5); security moved into Phase 1 entry criteria (§8). Tools renamed `cc_*` → `nelix_*`.

> **Line-number caveat:** code citations reference the Hermes source as of 2026-06-23. Line numbers
> drift between Hermes versions; the symbols and behaviors are the stable contract.

---

## 1. Problem Statement

Business context, goals, and success criteria live in the [Business Requirements](./business-requirements.md).
In one sentence: **Hermes orchestrates an arbitrary agentic executor CLI, driven from the user's
messaging channel, waking its LLM only at decision points — through one universal mechanism, not any
vendor-specific surface.**

### 1.1 Roles & Terminology

- **Hermes** — the Orchestrator Agent host. LLM-driven, turn-based. Nelix is a Hermes plugin.
- **Executor CLI** — any interactive agentic command-line tool being driven (vendor-neutral).
- **Host Daemon** — host-side, no-LLM process that owns the PTY + emulator + drivers + state.
- **Driver (per-tool)** — the per-executor-CLI integration (launch, readiness, classify, inject,
  resume, error taxonomy). The only tool-specific code (§3.5).
- **Messaging channel** — how the user talks to Hermes (vendor-neutral).
- **Orchestrator runtime** — the (possibly sandboxed / containerized) environment Hermes runs in,
  which may differ from the host where the executor CLI and its credentials live.
- **Decision point / event** — a moment the CLI needs input, or has finished / failed. Every event
  has a stable `event_id` and a monotonic per-session `event_seq` (§4).

### 1.2 Mediation principle (Hermes in the loop)

The executor CLI has **no channel to the user** — its only I/O is the PTY the daemon owns. Everything
the user sees passes through **Hermes' judgment**: the daemon distills terminal state into a compact
decision-point event, and Hermes' LLM decides what (and whether) to relay. Raw executor output is
never piped to the user. We deliberately do **not** give the executor — or any tool-specific wrapper —
its own direct messaging-channel integration (the rejected single-tool-daemon model, §6.4). The only
direct daemon→user path is the minimal Layer C fallback *nudge* (§4) — content-free, rate-limited,
audited; our code, not the CLI.

### 1.3 The universality bet (central design decision)

The defining constraint is **universality**: one orchestration mechanism must drive *any* agentic
executor CLI under *any* subscription. This rules out every vendor-specific integration surface, even
robust ones (a structured non-interactive/stream mode, executor-side hooks, a language-specific SDK —
§6).

The **only interface common to every CLI is the terminal itself** (stdin/stdout over a PTY) — the
classic `expect`/`pexpect` lineage. Therefore:

> **Universality lives in the transport (PTY) and a common tool abstraction
> (`nelix_start` / `nelix_respond` / `nelix_status` / `nelix_stop` / `nelix_restart`). Driving each
> CLI is irreducibly per-tool** — each renders "working" / "asking" / "done", resumes, and fails
> differently — so it is a **pluggable per-tool driver** behind a tool-agnostic core.

A precise restatement: not "drive an arbitrary CLI," but **"drive any CLI for which we write and
maintain a driver."** The driver is a *behavioral integration layer*, not just a screen classifier
(§3.5) — this per-tool cost is accepted as inherent and budgeted explicitly. Adding a CLI = writing a
driver; the orchestration core stays stable.

## 2. Requirements

### 2.1 Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-1 | User sends a development task via the messaging channel to Hermes | Must |
| FR-2 | Hermes spawns the target executor CLI in interactive mode over a PTY | Must |
| FR-3 | The CLI works autonomously between decision points — no LLM involvement during this time | Must |
| FR-4 | A per-tool driver (no LLM) classifies the CLI's rendered screen into decision points: question, permission request, completion, error | Must |
| FR-5 | On a decision point, the Hermes LLM is woken with a compact, id-addressed event summary (not raw terminal output) | Must |
| FR-6 | Hermes LLM decides how to respond and relays the question to the user via the messaging channel | Must |
| FR-7 | User answers → Hermes LLM interprets → `nelix_respond(session_id, event_id, answer)` injects it into the CLI via the PTY | Must |
| FR-8 | User can ask "how is it going?" at any time → Hermes calls `nelix_status` → returns current state + any pending event | Must |
| FR-9 | On completion, Hermes reports the result to the user | Must |
| FR-10 | The Hermes conversation loop must NOT block during long CLI tasks (e.g. 20+ min) — Hermes stays responsive | Must |
| FR-11 | Concurrent sessions (independent PTYs, workdirs) | **Post-MVP** (MVP enforces one active session, §3.7) |
| FR-12 | User can stop / cancel a running session, with defined graceful→force semantics and a post-stop status (§3.7) | Should |
| FR-13 | A running session survives **Hermes** restarts (owned by the host daemon, not by Hermes). Daemon-crash recovery is separate (§3.7) | Must |
| FR-14 | Restart the executor CLI process within a session — *fresh* (clean CLI session, same workdir) or *resume* (continue prior context where the driver supports it; **no silent downgrade**, §3.7) | Must |
| FR-15 | Detect context-window exhaustion where the driver can do so **explicitly**, and recover via a *fresh* session, not *resume*; heuristic-only suspicion is escalated to the user, not auto-acted (§3.6) | Must |
| FR-16 | On transient executor/server errors (provider downtime/5xx, rate-limit/429, network) recover autonomously with bounded **exponential backoff + jitter** — but **only while no side effects have been observed** since the error; otherwise escalate (§3.7) | Must |
| FR-17 | Decision delivery is **idempotent and id-addressed**: every event has `event_id` + monotonic `event_seq`; answers reference the event they resolve; duplicate/stale wake-ups are de-duplicated (§4) | Must |
| FR-18 | Answer injection is **sanitized for the executor's terminal/command surface** (bracketed paste where supported; strip control/ANSI/meta sequences; reject the CLI's command-prefix tokens after whitespace and every newline) (§5) | Must |

### 2.2 Non-Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| NFR-1 | **No Hermes-LLM tokens are consumed by routine executor progress** between decision points (the CLI works, the Hermes LLM sleeps) | Must |
| NFR-2 | Decision-point detection logic lives in per-tool drivers, not in LLM prompts | Must |
| NFR-3 | Works in Hermes **gateway mode** (persistent async service), not just interactive CLI mode | Must |
| NFR-4 | Executor interface is **universal**: PTY transport + per-tool driver. No reliance on any executor-CLI-specific contract (non-interactive mode, executor hooks, SDK) in the core | Must |
| NFR-5 | Uses the executor CLI's own auth (e.g. an OAuth subscription session in the OS credential store) — no API keys | Must |
| NFR-6 | The Hermes plugin is self-contained; **no modifications to Hermes core** | Must |
| NFR-7 | Runtime uses a **PTY + in-process terminal emulator (`pyte`)**, not `tmux capture-pane` polling | Must |
| NFR-8 | The host daemon (PTY owner) runs on the host where the executor CLI and its credentials live; the Hermes plugin is a thin RPC client (and may run in a separate orchestrator runtime) | Must |
| NFR-9 | The control plane is an RPC API (loopback HTTP or Unix socket), not shared-volume polling. Files are used for logs/transcripts and crash recovery only | Must |
| NFR-10 | Nelix secures **its own surfaces**: authenticated control plane, session authorization, answer-injection hygiene, secret redaction in outbound summaries, audit log (§5). **Nelix does NOT sandbox or restrict the executor CLI** (D2 trust model, §5). Auto-approval is post-MVP | Must |

### 2.3 Constraints

1. **Hermes is turn-based:** each turn = receive message → think → call tools → respond → end turn.
   No built-in async event loop. Between turns Hermes is free but cannot process events.

2. **Direct message injection does NOT work in gateway mode — VERIFIED.** Hermes'
   `PluginContext.inject_message()` reads `self._manager._cli_ref`; in gateway mode `_cli_ref is None`,
   so it logs a warning and returns `False` (the message is silently dropped). It is **not even listed
   in the public plugin API guide**. The async wake-up therefore must **not** use `inject_message`; it
   uses the background-process notification path (§4). *(`hermes_cli/plugins.py:409-433`.)*

3. **Verified PluginContext surface (the only API the plugin uses).** `register_tool`,
   `register_hook("pre_llm_call", …)`, `dispatch_tool`, `register_command`, `register_cli_command`,
   `register_skill`, and properties `profile_name` / `_cli_ref`. Tool handlers are functions
   `handler(args: dict, **kwargs) -> str` returning a JSON string (async handlers auto-detected). Full
   detail in **§3.9**.

4. **Background terminal processes are the async wake-up — VERIFIED to route in gateway mode.**
   Hermes' `terminal(background=true, notify_on_complete=true)` registers a gateway watcher that, on
   process exit, injects a synthetic internal message → a **new Hermes turn** carrying the process
   stdout. Works where `supports_async_delivery=True` (the gateway deployment); **unavailable on the
   stateless HTTP API**. The plugin reaches it via `dispatch_tool("terminal", …)`. Empirical
   reliability, lifetime, stdout limits, and timeout behavior are Phase-0 unknowns (§4, §7 #1, §8).
   *(`tools/terminal_tool.py:2299-2379`; `gateway/run.py:13177-13357`.)*

5. **Interactive executor CLIs run in a TUI.** A typical agentic CLI's interactive mode is a
   **full-screen terminal app** (e.g. `prompt_toolkit`: alternate screen, absolute cursor positioning,
   frequent redraws). A real PTY is required; reconstructing the rendered screen needs a terminal
   emulator (§3.3).

6. **No manual human takeover** of a session is required. This removes the main reason to use `tmux`
   (attachable sessions), enabling an in-process emulator instead.

7. **Orchestrator-runtime vs host split:** Hermes may run in a separate / sandboxed orchestrator
   runtime, while the executor CLI and its credentials live on the host. PTY ownership belongs on the
   host (§3.2).

8. **No vendor-specific integration in the core:** non-interactive modes, executor hooks, and SDKs are
   rejected for universality (§6). A driver MAY internally exploit a tool-specific signal, but the core
   must not depend on one.

## 3. Proposed Architecture

### 3.1 Core Concept

```
User (messaging channel)
   │  task
   ▼
Hermes (LLM, orchestrator)  ──RPC──►  Host Daemon (no LLM)
   ▲                                     │  owns: PTY child + pyte emulator
   │  id-addressed event                 │         + per-tool driver + state
   │  (only at decision points)          ▼
   │                                   Executor CLI (any agentic CLI)
   ▼
User (messaging channel)
```

- **Hermes:** LLM orchestrator. Thin RPC client to the daemon. Wakes only at decision points.
- **Host daemon:** the source of truth. Owns the PTY child, runs the terminal emulator, hosts the
  per-tool driver, persists state and transcripts, exposes the RPC API, emits wake-up events. No LLM.
- **Driver (inside the daemon, one per CLI):** the only tool-specific code (§3.5).

### 3.2 Topology & Boundary

```
┌────────── Orchestrator runtime (maybe sandboxed) ──────────┐   ┌──────────── Host ───────────────┐
│  Hermes (LLM)                                              │   │  Host daemon (no LLM)            │
│   └─ nelix plugin                                          │   │   ├─ RPC server (HTTP/Unix sock) │
│       └─ nelix_start / nelix_respond / nelix_status / ─────┼RPC┼──►├─ session manager (persist)    │
│          nelix_stop / nelix_restart  (thin RPC client)     │   │   ├─ PTY (ptyprocess) ─► CLI child│
└────────────────────────────────────────────────────────────┘   │   ├─ pyte emulator (grid)        │
                                                                  │   └─ per-tool driver (classify…) │
                                                                  └──────────────────────────────────┘
```

- **Why the daemon owns the PTY (NFR-8):** the executor CLI, its credentials, shell, and filesystem
  live natively on the host. Putting PTY ownership inside a sandboxed orchestrator runtime is the
  wrong coupling.
- **Why a daemon and not the plugin process (FR-13):** an in-process PTY child dies with its owner.
  Hermes is turn-based and may restart; the daemon is long-lived and decoupled from Hermes' turn
  lifecycle, so a session survives Hermes restarts.
- **Control plane = RPC, not files (NFR-9):** the daemon exposes loopback HTTP (reachable from the
  orchestrator runtime via the host address) or a Unix socket (mounted across the runtime boundary).
  Shared files are used only for transcripts/logs and crash recovery — never as the command/control
  channel.

### 3.3 Runtime: PTY + in-process emulator

Instead of `tmux` + periodic `capture-pane`:

```
CLI child  ──bytes──►  PTY (ptyprocess)  ──feed──►  pyte (VT emulator)  ──►  rendered screen grid
                                              │
                                              └──►  raw byte stream  ──►  full transcript log
```

Rationale (vs `tmux capture-pane` polling):
- `capture-pane` is a **lossy periodic screenshot** — it can miss transient states, conflate old/new
  contents, and forces a polling loop.
- `pyte` is an in-process VT-compatible emulator: feed it every byte, read a deterministic **rendered
  grid** on every read. This is **less lossy than polling** — but it is *not* lossless: the grid is
  still only the latest screen unless the daemon classifies on **semantic screen transitions** and
  retains event candidates (a prompt can appear and vanish between reads; redraws can clear text). The
  raw stream is logged for free.
- It handles **both** full-screen TUIs and line-oriented CLIs with one transport.

Caveat (accepted): terminal emulation is not free. The daemon must handle the alternate screen buffer,
**terminal resize** (`pyte` size tracks the PTY winsize; a resize is a **classifier reset** requiring
fresh stabilization — the driver pins a default PTY size and is tested at it), cursor positioning,
wrapped lines, and full-screen-TUI redraw quirks. `tmux` is retained only as an optional debug/escape
hatch, never the production runtime.

### 3.4 Tool Abstraction (Hermes plugin → daemon RPC)

| Tool | Type | Description |
|------|------|-------------|
| `nelix_start(task, workdir, tool, model?)` | Sync, returns immediately | RPC → daemon: spawn PTY child for `tool`, attach its driver, **arm the wake-up waiter** (§4), return `{operation, status, session_id, snapshot, next_after_seq, next_action}`. Optional `model` runs this session on a specific model — a tier alias (`haiku`/`sonnet`/`opus`) or a full model id; omit for the executor's configured default (start-time only; survives auto-restart). MVP rejects a start while another session is active (§3.7) |
| `nelix_respond(session_id, event_id, answer)` | Sync | RPC → daemon: validate the answer (§5), inject it via the PTY, mark the event `answered`, resume classification, re-arm the waiter. Rejects a stale/closed `event_id` |
| `nelix_status(session_id)` | Sync | RPC → daemon: return current driver state + the **canonical pending event** (if any) + last summary + duration. The reconciliation/fetch path (§4) |
| `nelix_stop(session_id)` | Sync | RPC → daemon: cancel the session — graceful interrupt → timeout → force kill — and return a post-stop status (§3.7) |
| `nelix_restart(session_id, mode, task?)` | Sync | RPC → daemon: terminate the current CLI child and re-spawn in the **same** session/workdir. `mode="fresh"` or `mode="resume"`; optional `task`. Re-arms the waiter. Returns `{operation, status, session_id, snapshot, next_after_seq, next_action}` (§3.7) |

All are thin RPC clients with **fast, non-blocking** handlers returning a JSON string — none blocks
waiting for the CLI. The long wait (until the next event) is handled out-of-band by the background
waiter (§4).

### 3.5 Driver (per-tool integration contract)

The adapter is a **per-tool driver** — a behavioral integration layer, not merely a classifier. Each
driver declares and implements a capability contract; the tool-agnostic core depends only on this
contract:

| Capability | What the driver provides |
|------------|--------------------------|
| `launch` | How to spawn the CLI (argv, env, default PTY size) |
| `readiness` | How to detect the CLI is ready for a task (the **initial prompt**) |
| `classify` | Map the rendered grid + frame history + liveness signals → a state (§3.6) |
| `inject_policy` | How to type an answer safely: bracketed-paste support, submit key, command-prefix tokens to reject, multiline rules (§5) |
| `resume` | `supported \| unsupported`; how to capture a resume handle at start and re-attach it on restart (§3.7) |
| `error_taxonomy` | Patterns for `server_error` (transient) vs fatal `crashed` vs auth failure |
| `context_exhaustion_detection` | `explicit \| heuristic \| unsupported` — only `explicit` is auto-acted (FR-15) |
| `redaction_hints` | Tool-specific regions/patterns likely to contain secrets |
| `destructive_recognition` | *(post-MVP)* recognizing destructive prompts for the auto-approve denylist (§5) |

Understating the driver as "just a classifier" was a known modeling error (per design review). Adding a CLI
means implementing this contract — the universality cost (§1.3).

**Launch constraint — the configured `command` must `exec` into the leaf CLI.** The daemon spawns
it under a bare PTY with no interactive job-control shell in front, so whatever is spawned owns the
terminal's **foreground process group**. A supervising wrapper that stays alive around the CLI
(any `<secret-fetch-command> -- <cli>` or `foo -- bar` shim that fork+waits) keeps the foreground
group for itself; the real CLI runs in a background group, takes `SIGTTIN`/`SIGTTOU` on first
terminal access and is **stopped** (`ps` STAT `T`), rendering nothing — the daemon then only sees a
blank screen. Such a wrapper works when a human runs it (their shell hands over the foreground) but
not here. Operators must fetch secrets and then `exec` the CLI so it *replaces* the wrapper. The
daemon does not manipulate the child's job-control state; it treats a leaf that never produces output
as a startup failure and bounds the wait rather than blocking forever (§3.7).

**Runtime-resolved launch env (`env_cmd`) — usually retires the wrapper (nelix-c5o).** A wrapper was
often needed only to fetch a runtime secret (an auth token that cannot be a plaintext literal in the
config) into the CLI's env. Instead, `[executors.<name>.env_cmd]` maps an env var to a command: at
spawn the daemon runs each command, uses its trimmed stdout (like shell `$(…)`) as the value, merges
it over the static `[env]`, and launches the leaf CLI **directly** — so the `command` is the
foreground leaf and the exec-into-leaf constraint holds without any script. nelix stays a dumb bridge
(it runs the operator's command and does not interpret the value; the value is the executor's *own*
auth, bound for its env regardless). A non-zero exit, timeout, or empty stdout is a clean start
failure, never a daemon crash. The resolved value is held only in memory at spawn — never logged,
persisted, or rendered by nelix's own sinks (§5) — and resolution re-runs on every spawn (incl.
restart), so nothing is cached and a rotated secret is picked up. This also lets nelix own the
complete launch env (endpoint + auth), unblocking endpoint-based model discovery (nelix-g9k).

### 3.6 State Classifier (inside the driver)

There is **no universal completion detector** without hooks or a structured protocol, and the design
does not chase one. The classifier is **confidence-ranked** over the rendered screen + a sliding-window
history of recent frames (ring buffer, default depth 30), captured **event-driven** (re-render on PTY
bytes, no fixed-rate polling), **plus** a **quiescence timer** (fires after X seconds of silence).

**Quiescence is suspicion, not classification.** Combine it with **liveness signals** the driver
exposes: process exit, child CPU/activity, input-mode/cursor position, recent command echo, and known
status regions. A stable-looking screen can hide network waits, throttled redraws, or a just-appeared
permission prompt.

The daemon persists per session: the current frame + a short diff summary + the latest event candidate
(not the full ring buffer).

| State | Meaning |
|-------|---------|
| `working` | spinner/progress, screen changing, or liveness shows activity |
| `waiting_for_user` | cursor in input area + explicit question / permission affordance |
| `idle_after_task` | input prompt restored, screen stable for N seconds, **and `task_accepted=true`** |
| `done_candidate` | stable task-idle prompt after observed tool activity (only if `task_accepted=true`) |
| `unknown_blocked` | no change for too long, screen matches no known state |
| `crashed` | process exit, traceback, auth failure, terminal error, command-not-found, etc. |

The daemon emits a wake-up event only when confidence crosses a threshold.

**Hard rules:**
1. **`task_accepted` gate.** The classifier MUST record an explicit `task_accepted` transition after a
   task is injected — evidence: task echo, a spinner appearing, or the first tool action. It MUST NOT
   emit `idle_after_task` / `done_candidate` while `task_accepted=false` (otherwise it falsely reports
   "done" right after start, after restart, or after a failed injection).
2. **Initial vs task-idle prompt.** Distinguish "ready for a task" from "returned to prompt after a
   task" using the `task_accepted` lifecycle, not screen text alone.
3. **Do not rely on natural-language completion text.** Combine: screen stable for X seconds **AND**
   input-prompt restored **AND** no spinner/progress **AND** no pending permission/question **AND**
   process alive **AND** `task_accepted=true`.
4. **Context exhaustion (FR-15).** Only when the driver declares `explicit` detection (a known
   "context limit" signature, an endless compaction loop) does the daemon flag `cause=context_exhausted`
   and recover with a **fresh** session (§3.7). `heuristic` suspicion is **escalated to the user**, not
   auto-acted.
5. **Transient errors (FR-16).** `cause=server_error` (provider 5xx, 429, connection/timeout) is
   **recoverable**, distinct from fatal `crashed`. Recovery is autonomous backoff under the safety rule
   in §3.7.
6. **`unknown_blocked` too long → escalate to the LLM** (let Hermes decide) rather than guessing.

State machine:

```
idle → working → { waiting_for_user | unknown_blocked | crashed | idle_after_task → done_candidate }
                          │                    │            │
                          │                    └────────────┴─ nelix_restart(fresh|resume) → working
                          └─ nelix_respond → working → …
```

### 3.7 Session Lifecycle & Restart

A Nelix session (one `session_id`) owns one executor-CLI process. **MVP runs one active session at a
time** — `nelix_start` rejects a new start while a session is active; the daemon keeps per-`session_id`
state so concurrency can be added post-MVP (FR-11). Controls beyond start/stop:

- **Restart in place** — `nelix_restart(session_id, mode, task?)` terminates the current CLI child
  (graceful → force) and re-spawns it in the **same** `session_id`/workdir, then re-arms the waiter:
  - `mode="fresh"` — clean CLI session; optional `task` (re)injects work.
  - `mode="resume"` — continue prior context via the **executor's own resume mechanism**, using the
    resume handle the driver captured at start and persisted with the session (so resume survives
    daemon restarts, and segments very long tasks — Unknown #5).
  - **No silent downgrade.** If the driver's `resume` capability is `unsupported`, `nelix_restart`
    returns `{"status":"resume_unsupported"}` and does **nothing** — Hermes/the user then chooses
    `fresh` explicitly (silent downgrade would falsely imply preserved context).
- **Recovery decisions:**
  - `crashed` / `unknown_blocked` → Hermes (or the user) chooses `resume` (keep progress) or `fresh`.
  - **Context exhaustion** → always `fresh` (or a brand-new session), never `resume` (resume reloads
    the overflow); optionally carry a short task summary into the fresh session.
  - **Transient `server_error` → autonomous backoff, with a safety gate:** the daemon retries with
    bounded **exponential backoff + jitter** — no LLM wake, no tokens (NFR-1) — **only while no side
    effects have been observed since the error** (no file changes, no commands run, no network actions
    visible in the transcript). After any observed side effect, it does **not** silently retry; it
    raises a decision event ("executor failed after modifying files — resume / fresh / stop?"). If the
    CLI does its own retrying, the daemon observes `working` and does nothing. On exhausting the retry
    budget, it escalates.
- **Daemon-crash recovery (separate from FR-13).** FR-13 guarantees survival of **Hermes** restarts
  only. If the **daemon** dies, its PTY children die with it; persisted summaries/transcripts allow a
  report but not seamless continuation. Daemon supervision (restart, child process groups, transcript
  replay into `pyte`, then `resume`/`fresh`) is **post-MVP** (§7 #5).

### 3.8 End-to-end Flow

```
1.  User → messaging channel: "refactor the auth module in ~/myproject"
2.  Hermes LLM → nelix_start(task="refactor auth module", workdir="~/myproject", tool="<executor>")
3.  Plugin → RPC → daemon: (reject if a session is already active) spawn PTY child, attach driver,
      detect initial prompt, inject the task text, begin classification.
    Daemon returns: {"status":"started","session_id":"abc123"}
    Plugin arms the wake-up waiter via dispatch_tool("terminal", background, notify_on_complete) (§4)
4.  Hermes LLM → messaging channel: "Started, working on it."   →   turn ends → Hermes FREE.

5.  [minutes pass; CLI works; daemon classifies on every PTY read; no LLM]

6.  Driver classifies waiting_for_user → daemon records event (event_id E1, seq 1).
      Waiter long-poll returns the opaque envelope "nelix_event abc123 E1" on stdout → it exits.
7.  notify_on_complete injects the envelope → new Hermes turn.
8.  Hermes LLM → nelix_status(abc123) → canonical event E1: {"type":"permission",
      "summary":"Delete old config.py?","prompt_type":"yes_no","options":["yes","no"]}
9.  MVP: Hermes relays E1 to the user (always ask — no auto-approval); turn ends (does NOT block).
10. User replies → new turn → nelix_respond(abc123, E1, "no") → daemon injects, marks E1 answered,
      re-arms the waiter.   →   turn ends → Hermes FREE.

11. User → "how's it going?" → nelix_status(abc123) → {"state":"working","summary":"Editing auth.py…"}
12. Driver classifies done_candidate (task_accepted=true) → event E2 → waiter envelope → Hermes woken.
13. Hermes LLM → nelix_status → E2 completion summary → "Done — refactor complete, 8 files changed."
```

### 3.9 Verified Hermes Plugin API Surface

Verified 2026-06-23 against the Hermes source, the official guide, and the example plugins.

| Capability | API | Notes |
|------------|-----|-------|
| Register the orchestration tools | `register_tool(name, schema, handler, toolset, …)` | `handler(args, **kwargs) -> str` returns JSON. Sync suffices; async auto-detected. *(`plugins.py:367`)* |
| Surface a pending event into a turn already in progress | `register_hook("pre_llm_call", cb)` | The **only** hook whose return value is used. Returns `{"context": "…"}`, **appended to the user message for that turn only** — never mutates saved history, preserves system-prompt caching. **Gateway == interactive behavior.** *(`agent/turn_context.py:366-391`; `plugins.py:1705-1740`, `:128-195`)* |
| Arm the async wake-up | `dispatch_tool("terminal", {command, background:true, notify_on_complete:true})` | The only path that registers the gateway completion watcher. The plugin must NOT spawn its own `subprocess`. *(`terminal_tool.py:2299-2379`; `gateway/run.py:13177-13357`)* |
| Optional in-session controls | `register_command(name, handler, …)` | `/nelix …`; async auto-detected. |
| Optional host CLI subcommand | `register_cli_command(…)` | Terminal only. |
| Execution-context detection | `ctx.profile_name`; `ctx._cli_ref is None` ⇒ gateway/headless | Prefer `dispatch_tool` over `_cli_ref`. |

**Two hard implementation constraints:**
1. **The waiter MUST go through the terminal tool** (`dispatch_tool("terminal", …)`), not a
   plugin-owned subprocess — only that path is picked up by the gateway watcher that injects the new
   turn. The waiter prints **only an opaque event envelope** to stdout (§4); summaries are fetched via
   `nelix_status`.
2. **`pre_llm_call` cannot wake Hermes from idle** — it fires only *within* an in-progress turn. It is
   Layer A reconciliation (§4), not an idle wake-up; idle is covered by the waiter and Layer C.

## 4. Wake-up Mechanism & Event Protocol

The async wake-up is the spine of the design. The mechanism is verified present and gateway-routed in
the Hermes source; the residual risks are *empirical reliability* and *terminal-tool lifetime/limits*
(§7 #1), to be measured in Phase 0.

**Event model.** Each session has a monotonic `event_seq`; each event a stable `event_id`. The daemon
holds events through a lifecycle: `delivered` → `user_visible` → `answered`/`resolved`. Events are
**acknowledged, not consumed**: a pending event persists until resolved, so reconciliation layers see
it without races. Completion/error events (no answer) are closed via `nelix_status` reporting or an
explicit `nelix_ack_event(event_id, disposition)`.

**Primary path.** On `nelix_start` / `nelix_respond` / `nelix_restart`, the plugin arms a background
**waiter** via `dispatch_tool("terminal", {background:true, notify_on_complete:true})`. The waiter
short-/bounded-long-polls the daemon (`/sessions/<id>/wait?after_seq=<n>`). On a new event the daemon
returns it; the waiter prints **only the opaque envelope** `nelix_event <session_id> <event_id>` to
stdout and exits → `notify_on_complete` delivers that envelope as a synthetic message → a new Hermes
turn. Hermes fetches the **canonical** event via `nelix_status` (no summaries, secrets, or large
payloads pass through stdout). On poll **timeout** the waiter prints **nothing** (or a heartbeat the
plugin ignores) and is re-armed. The plugin re-arms with `after_seq` = the last seen seq, so a slow old
waiter cannot resurrect a stale event; the daemon supersedes prior `waiter_generation`s.

**Reconciliation layers (defense in depth):**
- **Layer A — `pre_llm_call` hook (verified in gateway):** on any user-initiated turn, the hook
  queries pending events and, if one exists, appends a **structured, clearly-labeled** pending-event
  notice to that turn. If the user's current message is unrelated, the LLM is instructed to *ask before
  acting*, not silently switch context. Distinct from `inject_message` (mutates the current turn; never
  queues a message). Unreachable daemon ⇒ the hook **no-ops**. Cannot wake from idle.
- **Layer B — `nelix_status` on demand:** the user asks "how's it going?" → the LLM calls
  `nelix_status` → any pending event is returned. User-driven.
- **Layer C — periodic daemon notification (idle safety net):** if an event stays unresolved for T
  minutes (default 5), the daemon sends a **content-free, rate-limited, audited** nudge to the
  messaging channel — exact text: *"A Nelix session has a pending decision — message Hermes."* No
  CLI-derived content, no session secrets. The only direct daemon→user path (§1.2).

**Single-notification guarantee:** primary delivery and Layer C are mutually exclusive per `event_id`;
the id-based ack dedupes them so the user is pinged once.

## 5. Security Model (first-class)

### 5.1 Trust model (D2) — Nelix is not a sandbox

Nelix is a **single-operator, self-hosted** tool. **It does not sandbox or restrict the executor CLI:**
the CLI runs exactly as the operator configured it, with its own filesystem/network/command access and
its own permission model. Nelix does **not** interfere with the CLI's rights and is **not** a security
boundary around the CLI. ("Not a general remote shell" therefore means *Nelix exposes no shell of its
own*, not that the CLI is confined.) Real OS-level isolation of the executor — if ever wanted — is the
operator's responsibility (run the CLI in a container/sandbox) and is out of Nelix's scope.

Nelix secures only **its own surfaces** (below). The `workdir` is a launch parameter the operator
supplies; it is not enforced as a sandbox.

### 5.2 Controls Nelix owns

- **Always ask (MVP).** Every `waiting_for_user` event is relayed to the user; nothing is
  auto-confirmed. (Auto-approval is post-MVP, §5.3.)
- **Control-plane authentication.** The orchestrator-runtime→host RPC channel is authenticated (token /
  socket perms) and not exposed to the broader network.
- **Session authorization.** Which messaging-channel identity may drive Nelix sessions; ownership
  enforced.
- **Answer-injection hygiene (FR-18).** The injection target is the CLI's own stdin over the PTY — the
  threat is control/escape sequences, the CLI's own command syntax (slash-commands like `/exit`), and
  stray newlines that submit prematurely — **not** POSIX shell metacharacters. The driver's
  `inject_policy` (§3.5) governs:
  - Use **bracketed paste** where the CLI supports it; otherwise strip all control/ANSI/meta sequences.
  - `yes_no`: must match `^(y|n|yes|no)$`; `multiple_choice`: must be one of the extracted options —
    **prefer relaying enumerated options for the user to pick over free-form LLM-authored text** on
    risky prompts.
  - `free_text_short` (≤200) / `free_text_long` (≤2000): strip control/escape sequences; reject the
    CLI's command-prefix tokens after leading whitespace **and after every newline**; a single trailing
    Enter is added by the daemon; internal newlines only when the driver confirms a multi-line prompt.
- **Secret redaction & transcript handling.** Outbound summaries are scrubbed using the driver's
  `redaction_hints` before leaving the host. The **raw transcript** (§3.3) is more sensitive than
  summaries (tokens, file contents, env vars, OAuth URLs): it is stored with restricted permissions,
  retained per a defined policy, redacted at read, and **never sent to Hermes in raw chunks** without an
  explicit user request.
- **Audit log.** Every `nelix_start` / `nelix_respond` / `nelix_restart` / `nelix_stop`, every Layer C
  nudge, and (later) every auto-approval is logged with actor, session, event id, and decision.

### 5.3 Auto-approval (post-MVP)

Deferred — an LLM auto-approving prompts on a host shell is the most dangerous surface. When added:
deny-by-default + explicit allowlist; and a **destructive-operation override** (file deletion,
`git push --force`, credential access, package publish, network egress) that is **never** auto-approved
regardless of `prompt_type`. In the MVP this is moot — everything is relayed.

## 6. Rejected Approaches

> The operative reason for every rejection is **universality** (§1.3).

### 6.1 A structured non-interactive / stream mode (executor-specific)
Some CLIs emit structured output (e.g. stream-JSON) and accept structured input; robust where it
exists, but an **executor-specific contract** that does not generalize. Rejected for the core.

### 6.2 A language-specific Agent SDK (executor-specific)
Structured control for one tool, but executor-specific, adds a runtime dependency, and is a
vendor-lock-in surface (vendors can also change programmatic-use terms). Fails universality.

### 6.3 Executor-side hooks
Some CLIs expose hooks that can intercept/control flow — clean for *that* CLI, but executor-specific.
The core must not depend on them. *Unrelated to **Hermes** hooks (`pre_llm_call`, §3.9), which we do
use — by the orchestrator, not the executor.*

### 6.4 A third-party single-tool daemon
Turns one CLI into a standalone service with direct messaging-channel access — removing Hermes from the
loop; single-tool; bus-factor 1. Rejected (see §1.2).

### 6.5 `tmux` + `capture-pane` polling
Lossy periodic screenshots, missed transient states, mandatory polling; its one advantage (attachable
sessions) is moot (Constraint 6). Replaced by PTY + `pyte` (§3.3); `tmux` kept only as a debug hatch.

## 7. Critical Unknowns (validate before/while building)

| # | Unknown | Status | Fallback |
|---|---------|--------|----------|
| 1 | Async wake-up **reliability + terminal-tool lifetime/stdout-limits/timeout** in gateway mode | **Narrowed** — mechanism verified present & gateway-routed; reliability + lifetime are Phase-0 measurements | Layers B + C reconcile (§4); id-based ack dedupes; waiter prints nothing on timeout |
| 2 | Robust completion detection for a CLI that returns to an idle prompt instead of exiting | Open | `task_accepted` gate + liveness signals; escalate `unknown_blocked` to LLM (§3.6) |
| 3 | Orchestrator-runtime ↔ host RPC reachability & auth | Open | Loopback HTTP / mounted Unix socket; authenticated (§3.2, §5) |
| 4 | `pyte` faithfully renders the CLI's full-screen TUI (alt-screen, resize, wrapping) | Open | Pin PTY size per driver; resize = classifier reset; version-pin the CLI |
| 5 | Process/session lifetime; **daemon-crash recovery** of PTY children | Open | Daemon owns the long-lived child; supervision + transcript replay are post-MVP (§3.7) |
| 6 | Avoiding double-execution on resume after a transient error | **Addressed** — autonomous retry only while no side effects observed (§3.7) | Escalate a decision event after any observed side effect |
| ~~7~~ | ~~Synchronous pre-LLM hook in gateway mode?~~ | **RESOLVED — YES** (`pre_llm_call`, §3.9, §4) | — |

## 8. Implementation Plan

### Phase 0 — Spike the existential unknowns in isolation (throwaway)
Each spike validates one risk on its own, in the smallest possible harness:
1. **Wake-up + terminal-tool envelope.** Measure background-process lifetime, stdout size/delivery,
   exit-code handling, and timeout behavior in the live gateway; prove the envelope→`nelix_status`
   round-trip; watch for lost/duplicate notifications (Unknown #1).
2. **PTY + pyte against a real executor CLI.** Prove a minimal classifier distinguishes
   working / waiting / idle-after-task with the `task_accepted` gate at a pinned PTY size (Unknowns
   #2, #4).
3. **Orchestrator-runtime → host RPC**, authenticated (Unknown #3).

### Phase 1 — Walking skeleton (minimal, integrated, no Hermes tools)
A thin **end-to-end vertical slice** that proves the *integrated* mechanism is technically feasible —
before investing in the tool surface, security, or persistence. Driven by a **test harness / script
(or a single throwaway tool)**, NOT the polished `nelix_*` tool set; **no security, no persistence, no
restart, one hardcoded session.** Throwaway code is acceptable.
- Minimal daemon owns a PTY for one real executor CLI, feeds `pyte`, runs one driver's `classify`.
- Drive one full loop: inject a task → detect **one** `waiting_for_user` event → emit it through the
  real wake-up path (the actual gateway wake-up, not a unit test) → inject an answer back → observe the
  CLI continue → detect completion.
- **Exit criterion (go/no-go on feasibility):** the loop works against one real CLI, end to end.

### Phase 2 — Minimal plugin with tools + security baseline (one session)
Turn the skeleton into a real Hermes plugin.
- Plugin tools: `nelix_start`, `nelix_status` (thin RPC clients); wake-up waiter (opaque envelope)
  with Layer C wired.
- Driver v1: `launch` / `readiness` / `classify` (`working` / `waiting_for_user` / `done_candidate` /
  `crashed`) with the `task_accepted` gate; `inject_policy` for answer hygiene.
- **Security as entry criteria (not deferred):** control-plane auth, session authorization, audit log,
  answer-injection hygiene, secret redaction. Always-ask; one active session.

### Phase 3 — Full single-session orchestrator
- `nelix_respond` (id-addressed injection), `nelix_stop` (graceful→force + post-stop status),
  `nelix_restart` (fresh / resume, no silent downgrade).
- Full classifier incl. `idle_after_task` / `unknown_blocked` + LLM escalation; liveness signals;
  context-exhaustion (explicit) and transient-error backoff (side-effect-gated, §3.7).
- Event lifecycle + `nelix_ack_event`; Layer A `pre_llm_call` reconciliation.
- A second driver to validate the per-tool driver contract (§3.5).

### Post-MVP
- Concurrent sessions (FR-11); daemon-crash supervision + transcript replay (§3.7); deny-by-default
  **auto-approval** + destructive-operation override (§5.3).

## 9. Environment

- **Host:** a single self-hosted machine (24/7) running the host OS.
- **Hermes:** runs as a persistent service (gateway mode) exposing a messaging channel; its execution
  backend may run in a separate / sandboxed orchestrator runtime.
- **Host daemon:** on the host where the executor CLI and its credentials live (owns PTY + pyte +
  drivers).
- **Executor:** any agentic CLI on the host, using its own authenticated session (e.g. an OAuth
  subscription in the OS credential store), running with its own configured permissions (§5.1).
- **Messaging:** via Hermes (and the daemon's content-free Layer C nudge).
- **Control plane:** authenticated local RPC — loopback HTTP via the host address, or a mounted Unix
  socket.

## 10. Open Questions / Pending Decisions

| # | Decision | Options / Notes | Resolve by |
|---|----------|-----------------|------------|
| Q1 | RPC transport | Loopback HTTP via the host address vs mounted Unix socket | Phase 0 spike #3 |
| Q2 | Repo layout | Single repo with `daemon/` + `plugin/` packages vs split | Before Phase 1 |
| Q3 | First-plan scope | Phase 0 only vs Phase 0 + Phase 1 (walking skeleton) — input to `writing-plans` | Before writing-plans |
| Q4 | Waiter protocol | Bounded long-poll vs short polls + heartbeat; max-poll duration N; re-arm cadence; `after_seq` semantics | Phase 0 spike #1 |
| Q5 | Backoff tunables | Base delay, multiplier, max retries, jitter, cap; and how the daemon distinguishes a transient stall from `unknown_blocked` and from a side-effecting failure | Phase 3 |
| Q6 | "No side effects observed" detection | How the daemon detects side effects since an error (working-tree diff, command echo, transcript scan) to gate autonomous resume (§3.7) | Phase 3 |
