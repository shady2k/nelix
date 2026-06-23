"""Throwaway: spawn the configured executor over a PTY, render with pyte, dump frames.

Launch comes from nelix.toml (NELIX_CONFIG / NELIX_EXECUTOR); nothing is hardcoded.
Usage: .venv/bin/python spikes/spike_b_pty_pyte.py
"""
import os
import select
import time
from pathlib import Path

import pyte
import ptyprocess

from daemon.config import load_executors

COLS, ROWS = 120, 40


def render(screen):
    return "\n".join(screen.display)


def main():
    execs = load_executors(os.environ.get("NELIX_CONFIG", "nelix.toml"))
    name = os.environ.get("NELIX_EXECUTOR") or next(iter(execs))
    spec = execs[name]
    os.makedirs(spec.resolved_cwd(), exist_ok=True)

    out = Path("spikes/frames")
    out.mkdir(parents=True, exist_ok=True)
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.ByteStream(screen)
    child = ptyprocess.PtyProcess.spawn(
        spec.argv(), dimensions=(ROWS, COLS),
        cwd=spec.resolved_cwd(), env=spec.resolved_env(),
    )
    n, last = 0, time.time()
    try:
        while child.isalive():
            r, _, _ = select.select([child.fd], [], [], 0.1)
            if r:
                try:
                    stream.feed(child.read(65536))
                except EOFError:
                    break
                last = time.time()
            elif time.time() - last > 0.3:
                (out / f"{n:03d}.txt").write_text(render(screen))
                n += 1
                last = time.time()
    finally:
        child.close(force=True)


if __name__ == "__main__":
    main()
