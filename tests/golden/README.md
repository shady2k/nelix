# Golden frames — driver-conformance harness

Curated rendered CLI frames (excerpts — trimmed to the lines `classify()` keys on) + their expected
`classify()` result. The directory name under `claude/` IS the expected result:
`claude/<expected-classify>/<name>.txt`. `tests/test_driver_
conformance.py` asserts `ClaudeDriver().classify(frame, settled_ctx) == <dir name>` for every file.

When Claude Code changes how it draws the screen (a new spinner footer, a renamed marker), the driver
can silently start misclassifying a live agent (that was `nelix-48o`: a working agent read as idle).
A red conformance test catches that class of drift in dev instead of in production.

## Capturing / refreshing frames

The daemon persists each session's raw PTY bytes at `sessions/<id>/raw` (+ `meta.json` with the
cols/rows it ran at). `bin/nelix-capture` replays that raw through the **same** renderer the daemon
uses, so a captured frame is exactly what `classify()` sees — no live process needed.

```sh
# inspect a session's frames (dims come from meta.json):
bin/nelix-capture ~/.hermes/.../sessions/<id> --all            # gallery of distinct frames
bin/nelix-capture ~/.hermes/.../sessions/<id> --at-marker Cultivating   # a specific spinner frame
bin/nelix-capture ~/.hermes/.../sessions/<id> --final          # the last screen (idle / menu / done)

# a bare raw file works too (pass the dims it was captured at):
bin/nelix-capture path/to/raw --cols 120 --rows 40 --final
```

Only `--final` is fully faithful (pyte state after all bytes). `--at-marker` / `--all` snapshot pyte
state at byte *prefixes* — plausible screens you still curate by eye, not provably ones the daemon
classified at that instant (raw has no inter-byte timing, so "stable for N seconds" isn't replayable;
the test simulates stability with `stable_for=9.9`).

## The update protocol — NOT "refresh until green"

A green harness is worthless if the goldens don't cover the drifted state. So:

1. Run a real session that exhibits the state (use a **generic, non-sensitive task** — frames are
   committed to a public repo; do not capture project content into them).
2. `nelix-capture` it to get the candidate frame(s).
3. **A human decides which class the frame belongs to** and saves it under that class dir, trimmed to
   the relevant lines (status line + input box + footer; scrollback is irrelevant to `classify`).
4. Run `pytest tests/test_driver_conformance.py`.
5. If red, it is one of two things — decide which:
   - a **real driver bug** → fix `daemon/drivers/claude.py` (the golden's expected class is right);
   - a **legitimate CLI change** → update the driver AND, only if the *human classification* of the
     screen genuinely changed, the expected class. Never move a frame to a different class merely to
     silence the test.

## Scope (v1)

Only `classify()` is asserted, against `working` / `idle_prompt` / `permission_prompt` at a settled,
alive ctx — the decision-point states where drift bites. The driver primitives
(`is_accepting_input` / `is_modal_choice` / `input_submission_present`), `crashed`/`exited`, and
per-frame ctx overrides are deferred (they would arrive as a `manifest.toml` beside the frames).

> Note: the trust menu (`1. Yes` / `2. No, exit`) is handled in production by `is_modal_choice`, not
> by `classify()` (which returns `idle_prompt` for it). It therefore belongs to the deferred
> primitive-conformance set, not to `permission_prompt/` here.

## Engine note (Phase 1 — libghostty-vt)

The daemon now renders via **libghostty-vt** (`make_renderer` in `daemon/pty_session.py`); pyte is
removed.  `bin/nelix-capture` replays raw through the same engine, so captured frames remain exactly
what `classify()` sees.

The existing 9 golden `.txt` frames under `working/`, `idle_prompt/`, and `permission_prompt/` remain
valid: `test_driver_conformance.py` asserts `classify()` against their text content, which is
engine-independent (the driver keys on text tokens, not rendering internals).

`claude/_regression/3p1_alt_screen.raw` is the faithful-render regression fixture (session
s-e54b456e, 120x40, ~1.8 MB).  `tests/test_render_3p1.py` asserts that libghostty-vt renders this
Claude Code alt-screen capture cleanly — pyte rendered 24/40 rows with stale/overlapping chars;
ghostty renders all 40 rows intact (validated against xterm.js in spike nelix-ks5).
