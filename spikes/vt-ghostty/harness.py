#!/usr/bin/env python3
"""Drive libghostty-vt (shim.wasm) from Python via wasmtime: feed a captured PTY raw,
format the active screen as plain text, and report cursor / alt-screen state.

This is the in-process, no-Node, pip-installable path the spike validates: the only
runtime dependency is `pip install wasmtime` plus the bundled shim.wasm (built by
build.sh; Zig is build-time only). Importable: `from harness import render_ghostty`.
"""
import argparse
import os
import time

from wasmtime import Engine, Store, Module, Instance, Func, FuncType, ValType

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WASM = os.path.join(HERE, ".build", "shim.wasm")
INBUF_CAP = 2 << 20  # must match shim.c INBUF


def render_ghostty(raw: bytes, wasm_path: str = DEFAULT_WASM, cols: int = 120, rows: int = 40):
    """Render `raw` through libghostty-vt and return (screen_text, info_dict).

    A fresh wasm instance per call keeps renders independent (no shared state)."""
    if len(raw) > INBUF_CAP:
        raise RuntimeError(f"raw is {len(raw)} bytes; exceeds shim INBUF ({INBUF_CAP})")
    if not (0 < cols <= 0xFFFF and 0 < rows <= 0xFFFF):
        raise ValueError(f"cols/rows must be in 1..65535 (got {cols}x{rows})")
    engine = Engine()
    store = Store(engine)
    module = Module.from_file(engine, wasm_path)
    # The lib imports env.log(ptr, len) for internal diagnostics; a no-op satisfies it.
    log = Func(store, FuncType([ValType.i32(), ValType.i32()], []), lambda a, b: None)
    inst = Instance(store, module, [log])
    ex = inst.exports(store)
    mem = ex["memory"]

    def call(name, *a):
        return ex[name](store, *a)

    r = call("spike_new", cols, rows)
    if r != 0:
        raise RuntimeError(f"spike_new failed: GhosttyResult={r}")
    mem.write(store, raw, call("spike_inbuf"))
    t0 = time.perf_counter()
    call("spike_write_n", len(raw))
    feed_ms = (time.perf_counter() - t0) * 1000
    n = call("spike_format")
    if n < 0:
        raise RuntimeError(f"spike_format failed: {n}")
    outbuf = call("spike_outbuf")
    text = bytes(mem.read(store, outbuf, outbuf + n)).decode("utf-8", "replace")
    info = {
        "feed_ms": feed_ms,
        "cursor": (call("spike_cursor_x"), call("spike_cursor_y")),
        "cursor_visible": call("spike_cursor_visible"),
        "active_screen": call("spike_active_screen"),  # 0=primary, 1=alternate
        "mem_kb": mem.size(store) * 64,
    }
    return text, info


def main():
    ap = argparse.ArgumentParser(description="Render a captured PTY raw via libghostty-vt (wasm).")
    ap.add_argument("raw", help="path to a captured session `raw` byte stream")
    ap.add_argument("--wasm", default=DEFAULT_WASM, help="path to shim.wasm (default: .build/shim.wasm)")
    ap.add_argument("--out", help="write the rendered screen to this file instead of stdout")
    ap.add_argument("--cols", type=int, default=120)
    ap.add_argument("--rows", type=int, default=40)
    args = ap.parse_args()

    with open(args.raw, "rb") as fh:
        raw = fh.read()
    text, info = render_ghostty(raw, args.wasm, args.cols, args.rows)
    text2, _ = render_ghostty(raw, args.wasm, args.cols, args.rows)  # determinism check

    print(
        f"feed_ms={info['feed_ms']:.1f}  cursor={info['cursor']}  visible={info['cursor_visible']}"
        f"  active_screen={info['active_screen']} (1=alternate)  mem={info['mem_kb']}KB"
    )
    print(f"deterministic (double-render identical): {text == text2}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
    else:
        print(text)


if __name__ == "__main__":
    main()
