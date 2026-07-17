# Spike: faithful VT rendering via libghostty-vt (wasm, in-process)

**Status:** ✅ green. De-risks the production renderer choice. Not wired into nelix yet.

## Why

nelix relays the agent's terminal screen by reconstructing it from the raw PTY byte
stream with **pyte**. For full-screen TUIs that use the alternate screen + synchronized
output (DEC 2026) + scroll regions + heavy cursor-addressed diff repaint (e.g. Claude
Code), pyte's emulation is incomplete and the reconstructed screen **garbles**.

This spike proves the fix direction: swap the screen reconstructor for a *complete* VT
engine, keeping the rest of nelix (PTY ownership, drivers, RPC) unchanged.

## What it shows

Feeding one captured 1.8 MB alt-screen session (`s-e54b456e`) at 120×40 through three
engines:

| Engine | Result |
| --- | --- |
| **pyte 0.8.2** (current) | 24/40 rows garbled |
| **xterm.js** (node, reference) | clean |
| **libghostty-vt** (this spike, wasm in Python) | clean — **0/40 rows differ from xterm.js** |

Two independent faithful engines agreeing **row-for-row** (0/40 rows differ, after trailing-
whitespace trim — `compare.py` trims each row, it does not assert raw byte equality) is the
strong signal: the cause is pyte's incompleteness, and a complete engine renders the same
bytes correctly. The libghostty-vt path is **in-process, Node-free, deterministic**
(double-render identical), and fast (~43 ms to feed 1.8 MB).

## Architecture

- `shim.c` — a tiny flat-ABI C shim over libghostty-vt. The C compiler owns the nested
  sized-struct ABI; Python only ever calls scalar functions (`spike_new`, `spike_write_n`,
  `spike_format`, cursor getters). This is the prototype of the eventual nelix `Renderer`
  adapter. Compiled to `wasm32-freestanding` and linked against `libghostty-vt.a`.
- `harness.py` — drives `shim.wasm` from Python via [`wasmtime`](https://pypi.org/project/wasmtime/):
  writes raw bytes into wasm memory, feeds them, reads back the formatted plain-text screen.
- `compare.py` — diffs the libghostty-vt render against pyte (and optionally an xterm dump).
- `build.sh` — reproducible build of `.build/shim.wasm` (pins Zig 0.15.2 + a ghostty commit,
  both fetched locally into `.build/`, which is gitignored).

**In production the runtime dependency would be only `pip install wasmtime` + a prebuilt,
pinned `.wasm` shipped in the package** — Zig is *build-time only* (ours, to produce the
artifact), never needed by nelix users. Note: this spike does **not** commit the `.wasm`
(it lands in the gitignored `.build/`); run `make vt-spike-build` to produce it locally.

## Reproduce

```bash
make vt-spike-build                      # builds .build/shim.wasm (downloads pinned Zig + ghostty)
make vt-spike-run RAW=<path-to-session-raw>   # renders RAW via libghostty-vt and diffs vs pyte
```

`RAW` is a captured session byte stream, e.g.
`$NELIX_HOME/sessions/<id>/raw` (default `~/.nelix/sessions/<id>/raw`).

For the full per-row view: `.venv/bin/python spikes/vt-ghostty/compare.py <RAW> --show`.

## Pins

- Zig **0.15.2** (ghostty requires exactly this; newer Zig is rejected at configure time).
- ghostty **07d31666e73bce337b9cece60a884c67fe8906f4** (2026-06-27).
- libghostty-vt API is explicitly unstable — treat the built `.wasm` as a pinned artifact.
