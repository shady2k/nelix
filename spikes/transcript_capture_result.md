# Spike D — transcript capture verdict

**Outcome: FAIL** (repaint-only / alt-screen TUI). Task 4 uses the **per-stop snapshot**
line-source (the documented fallback), accepting screen-height loss. The spec's fidelity
caveat is re-confirmed and tightened (see "Elision" below).

Date: 2026-06-24 · Executor: Claude Code v2.1.187 driving `glm-5.2[1m]` via z.ai
(`envconsul -secret=homelab/zai -- zai-wrapper.sh`) · Screen: 120×40 · Task: print 1..60.

## What the spike returned

```
raw_bytes=14438 history_lines=0 viewport_lines=40
```

`history_lines=0` and the final viewport showed a non-contiguous slice (`1-9, 13, 14,
54-60`) — the FAIL signature exactly.

## Root cause (confirmed against the raw PTY capture `_spike_d_raw`)

The TUI is an **alternate-screen, cursor-addressed, repaint-only** application:

| Marker | Count | Meaning |
|--------|-------|---------|
| `\x1b[?1049h` (enter alt-screen) | 1 (never exited) | runs in the alternate buffer |
| `\x1b[H` (cursor-home) | 111 | repaints a fixed region in place |
| literal `LF` (`\n`) bytes | **0** | nothing ever scrolls the normal buffer |
| `\x1b[2J` (clear screen) | 1 | full clear at startup |

pyte `HistoryScreen.history` only accumulates lines that scroll off the **top of the
normal buffer** via linefeed/index. Alt-screen active + zero LFs ⇒ no line ever scrolls
off ⇒ `history.top` is empty ⇒ `history_lines=0`. This is a property of the **executor**,
not the capture code — no capture-side fix changes it.

## Elision (tightens the fidelity caveat)

The captured frame was a **settled idle frame** (`Cooked for 9s` + the `❯` input box
present), not a mid-stream frame — yet the 60-line answer rendered as a non-contiguous
slice that fit within 40 rows. Claude Code **elides/collapses a long message in its own
rendering**. Consequence: the per-stop snapshot does **not** faithfully reconstruct a long
turn's full body even *within* screen height; it faithfully captures the **decision point**
(the question / permission box at the bottom), which is what nelix needs. Faithful
long-turn bodies require the deferred structured-output / stream-json driver mode
(explicit non-goal of this spec).

## Decision for the build

- **Task 4**: keep `pump()`'s raw tee + `render()`; make `_commit_scrolled` a **no-op**;
  rely on `flush_viewport` at each stop (Task 7) to snapshot the viewport as the turn's
  lines. Everything downstream (Dialog, paging, RPC) is unchanged.
- **Fidelity caveat**: downgraded for the Claude driver — `nelix_dialog` history for long
  turns is best-effort per-stop snapshots, not a full transcript.
- Fallback chain options (b) driver-level extraction and (c) structured-output mode remain
  deferred.

Artifacts: `_spike_d_raw` (14438 bytes, forensic ground truth), `_spike_d_transcript.txt`
(reconstructed = viewport only, history empty).
