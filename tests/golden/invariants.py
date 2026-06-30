"""Machine-checkable registry of spec §4 invariants (nelix-5gc).

Each entry traces to a specific bug-fix commit and drives the real-capture
harvest in subsequent tasks.  No production code is touched by this module.

Tier meanings:
  1 — frame-level observe(frame, ctx) — pure classification / renderer fidelity
  2 — sequence (raw-replay across the full frame stream)
  3 — session-loop / retained synthetic

Kind meanings:
  "frame"     — single real harvested frame
  "sequence"  — raw-replay across frame stream
  "session"   — Session._loop() glue
  "synthetic" — intentional failure-injection; real negative capture unavailable
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Invariant:
    id: str
    tier: int
    kind: str
    bug_commit: str
    description: str


INVARIANTS: tuple[Invariant, ...] = (
    # ------------------------------------------------------------------ Tier 1 — frame-level
    Invariant(
        id="I1",
        tier=1,
        kind="frame",
        bug_commit="3f7f6d7",
        description=(
            "NBSP paste placeholder — on the real delivery capture, "
            "submitted_echo_present becomes true; the NBSP placeholder is recognized."
        ),
    ),
    Invariant(
        id="I2a",
        tier=1,
        kind="frame",
        bug_commit="cd3352d",
        description=(
            "bg-subagent driver frame — prompt_kind none, "
            "busy_reason waiting_subagents, heartbeat present, no accepts_text_input affordance."
        ),
    ),
    Invariant(
        id="I3",
        tier=1,
        kind="frame",
        bug_commit="e80fbcc",
        description="Real working-spinner frames → prompt_kind none.",
    ),
    Invariant(
        id="I4a",
        tier=1,
        kind="frame",
        bug_commit="68d6c7c",
        description=(
            "Footer-gate — bare ❯ with no footer → prompt_kind unknown, not free_text; "
            "a real free-text prompt with footer → free_text."
        ),
    ),
    Invariant(
        id="I5",
        tier=1,
        kind="frame",
        bug_commit="f3d89dc",
        description=(
            "Transcript chrome — a real transient tool-status row (ellipsis/⏺/ctrl+b) "
            "is_transcript_volatile; a settled content row with similar glyphs is not. "
            "Both halves required."
        ),
    ),
    Invariant(
        id="I6a",
        tier=1,
        kind="frame",
        bug_commit="8ecb2f5",
        description=(
            "Modal driver — a real numbered modal → prompt_kind modal/permission "
            "with selectable options carrying ids."
        ),
    ),
    Invariant(
        id="I-R1",
        tier=1,
        kind="frame",
        bug_commit="9164bf6",
        description=(
            "Kitty CSI renderer — replaying a real raw with a kitty-keyboard CSI "
            "produces no stray 'u' in the rendered frame."
        ),
    ),
    Invariant(
        id="I-R2",
        tier=1,
        kind="frame",
        bug_commit="f24dd9e",
        description=(
            "Ghostty renderer — 3p1_alt_screen.raw renders a clean 40-row viewport "
            "with known-intact lines."
        ),
    ),
    Invariant(
        id="I-AM",
        tier=1,
        kind="frame",
        bug_commit="d874852",
        description=(
            "Ask/auto mode — a real ask-mode frame → ask_mode true; "
            "a real auto/accept frame → ask_mode false (claude.py:199)."
        ),
    ),
    Invariant(
        id="I-BC",
        tier=1,
        kind="frame",
        bug_commit="c52c5cc",
        description=(
            "Bash-command chrome — busy_reason running_command from a real "
            "Bash( tool panel (claude.py:225)."
        ),
    ),
    # ------------------------------------------------------------------ Tier 2 — sequence
    Invariant(
        id="I2b",
        tier=2,
        kind="sequence",
        bug_commit="cd3352d",
        description=(
            "bg-subagent session — replaying the bg-subagent capture "
            "never publishes waiting_for_user."
        ),
    ),
    Invariant(
        id="I4b",
        tier=2,
        kind="sequence",
        bug_commit="68d6c7c",
        description=(
            "Echo region — submitted text appearing in scrollback/output does not set "
            "submitted_echo_present; the same text in the active input box does. "
            "Requires ctx.last_submitted_text plus a real capture containing both."
        ),
    ),
    Invariant(
        id="I7",
        tier=2,
        kind="sequence",
        bug_commit="6e0b8c6",
        description=(
            "Re-mint / double-ask — replaying the real capture publishes "
            "the blocked decision once, not N times."
        ),
    ),
    Invariant(
        id="I8",
        tier=2,
        kind="sequence",
        bug_commit="6de482c",
        description=(
            "False-idle — post-submit, a real repaint does not publish waiting_for_user."
        ),
    ),
    # ------------------------------------------------------------------ Tier 3 — session glue / synthetic
    Invariant(
        id="I6b",
        tier=3,
        kind="session",
        bug_commit="8ecb2f5",
        description=(
            "Modal respond routing — a modal decision answer drives "
            "driver.select_option(id), appends the option label, and rejects an invalid id "
            "(session.py:853)."
        ),
    ),
    Invariant(
        id="I9",
        tier=3,
        kind="synthetic",
        bug_commit="2f7fac4",
        description=(
            "Submit-confirm — a free-text submit is confirmed only with positive "
            "post-write evidence (the answer left the box). "
            "Negative states (dropped Enter, stale pre-write, never-echoes, "
            "ambiguous-then-stranded) stay synthetic."
        ),
    ),
)
