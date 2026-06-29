# Golden frames — driver-conformance harness

Curated rendered CLI frames (excerpts — trimmed to the lines `observe()` keys on) + their expected
read. The directory name under `claude/` is the OLD six-state label, REMAPPED to the new
`prompt_kind` vocabulary: `working -> none`, `idle_prompt -> free_text`,
`permission_prompt -> {permission_choice | modal_choice}`. `tests/test_driver_conformance.py` asserts
`ClaudeDriver().observe(frame, ctx).prompt_kind` is in the remapped set for every file.

When Claude Code changes how it draws the screen (a new spinner footer, a renamed marker), the driver
can silently start misreading a live agent (that was `nelix-48o`: a working agent read as idle).
A red conformance test catches that class of drift in dev instead of in production.

## Capturing / refreshing frames

The daemon persists each session's raw PTY bytes at `sessions/<id>/raw` (+ `meta.json` with the
cols/rows it ran at). `bin/nelix-capture` replays that raw through the **same** renderer the daemon
uses, so a captured frame is exactly what `observe()` sees — no live process needed.

```sh
# inspect a session's frames (dims come from meta.json):
bin/nelix-capture ~/.hermes/.../sessions/<id> --all            # gallery of distinct frames
bin/nelix-capture ~/.hermes/.../sessions/<id> --at-marker Cultivating   # a specific spinner frame
bin/nelix-capture ~/.hermes/.../sessions/<id> --final          # the last screen (idle / menu / done)

# a bare raw file works too (pass the dims it was captured at):
bin/nelix-capture path/to/raw --cols 120 --rows 40 --final
```

Only `--final` is fully faithful (renderer state after all bytes). `--at-marker` / `--all` snapshot renderer
state at byte *prefixes* — plausible screens you still curate by eye, not provably ones the daemon
read at that instant. Timing/liveness are core-owned now (the BeliefEngine reads an injected clock),
so the pure observe() conformance asserts only the frame read (`prompt_kind`), not temporal settle;
the timestamped golden-capture replay (`tests/test_replay_trail.py`) covers the temporal behavior.

## The update protocol — NOT "refresh until green"

A green harness is worthless if the goldens don't cover the drifted state. So:

1. Run a real session that exhibits the state (use a **generic, non-sensitive task** — frames are
   committed to a public repo; do not capture project content into them).
2. `nelix-capture` it to get the candidate frame(s).
3. **A human decides which class the frame belongs to** and saves it under that class dir, trimmed to
   the relevant lines (status line + input box + footer; scrollback is irrelevant to `observe`).
4. Run `pytest tests/test_driver_conformance.py`.
5. If red, it is one of two things — decide which:
   - a **real driver bug** → fix `daemon/drivers/claude.py` (the golden's expected class is right);
   - a **legitimate CLI change** → update the driver AND, only if the *human classification* of the
     screen genuinely changed, the expected class. Never move a frame to a different class merely to
     silence the test.

## Scope (v1)

`observe(frame, ctx).prompt_kind` is asserted, with the old dir names remapped (`working -> none`,
`idle_prompt -> free_text`, `permission_prompt -> {permission_choice | modal_choice}`) at an
alive ctx. The rest of the Observation contract (affordances, options, fingerprints, heartbeat,
submitted_echo_present) is covered by `tests/test_driver_claude_observe.py`; crash/exit derive from
`ObservationCtx`.

> Note: the trust menu (`1. Yes` / `2. No, exit`) is a numbered modal — `observe()` returns
> `modal_choice` (a Yes/No permission gate would be `permission_choice`). Both surface as a choice
> with options and route through `select_option`.

## Engine note (Phase 1 — libghostty-vt)

The daemon now renders via **libghostty-vt** (`make_renderer` in `daemon/pty_session.py`); pyte is
removed.  `bin/nelix-capture` replays raw through the same engine, so captured frames remain exactly
what `observe()` sees.

The existing 9 golden `.txt` frames under `working/`, `idle_prompt/`, and `permission_prompt/` remain
valid: `test_driver_conformance.py` asserts `observe()` against their text content, which is
engine-independent (the driver keys on text tokens, not rendering internals).

`claude/_regression/3p1_alt_screen.raw` is the faithful-render regression fixture (session
s-e54b456e, 120x40, ~1.8 MB).  `tests/test_render_3p1.py` asserts that libghostty-vt renders this
Claude Code alt-screen capture cleanly — pyte rendered 24/40 rows with stale/overlapping chars;
ghostty renders all 40 rows intact (validated against xterm.js in spike nelix-ks5).
