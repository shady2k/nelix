#!/usr/bin/env python3
"""Compare the faithful engine (libghostty-vt via wasm) against pyte (nelix's current
engine) on a captured PTY raw, row-for-row. Optionally also diff against an xterm.js
dump (--xterm) as a second independent faithful reference.

Result on the capture this spike was built from (s-e54b456e, 1.8 MB alt-screen TUI):
  ghostty vs pyte  : 24/40 rows differ  (pyte garbles)
  ghostty vs xterm : 0/40  rows differ  (two faithful engines agree; rows trailing-trimmed)
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, REPO)   # daemon.pty_session
sys.path.insert(0, HERE)   # harness.py (local, must win over any repo-root module)

from harness import render_ghostty, DEFAULT_WASM  # noqa: E402
from daemon.pty_session import render_raw          # noqa: E402


def rows(text):
    return [line.rstrip() for line in text.rstrip("\n").split("\n")]


def differing(a, b):
    n = max(len(a), len(b))
    a = a + [""] * (n - len(a))
    b = b + [""] * (n - len(b))
    return [i for i in range(n) if a[i] != b[i]]


def main():
    ap = argparse.ArgumentParser(description="libghostty-vt vs pyte on a captured raw.")
    ap.add_argument("raw", help="path to a captured session `raw` byte stream")
    ap.add_argument("--wasm", default=DEFAULT_WASM)
    ap.add_argument("--cols", type=int, default=120)
    ap.add_argument("--rows", type=int, default=40)
    ap.add_argument("--xterm", help="optional xterm.js plain-text dump to also compare against")
    ap.add_argument("--show", action="store_true", help="print the ghostty screen and the rows that differ from pyte")
    args = ap.parse_args()

    with open(args.raw, "rb") as fh:
        raw = fh.read()

    g_text, info = render_ghostty(raw, args.wasm, args.cols, args.rows)
    g = rows(g_text)
    p = rows(render_raw(raw, args.cols, args.rows))

    print(
        f"libghostty-vt: cursor={info['cursor']}  active_screen={info['active_screen']} (1=alt)"
        f"  feed_ms={info['feed_ms']:.1f}"
    )
    gp = differing(g, p)
    print(f"ghostty vs pyte : {len(gp)}/{max(len(g), len(p))} rows differ")
    if args.xterm:
        with open(args.xterm, encoding="utf-8") as fh:
            x = rows(fh.read())
        gx = differing(g, x)
        print(f"ghostty vs xterm: {len(gx)}/{max(len(g), len(x))} rows differ")

    if args.show:
        print("\n--- libghostty-vt screen ---")
        for i, line in enumerate(g):
            print(f"{i:>2}|{line}")
        print("\n--- rows where ghostty != pyte ---")
        for i in gp:
            pyte_row = p[i] if i < len(p) else ""
            print(f"row {i}:\n  ghostty: {g[i]!r}\n  pyte   : {pyte_row!r}")


if __name__ == "__main__":
    main()
