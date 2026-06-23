"""Interactive throwaway spike: run the configured executor over a PTY, MIRROR it to your
terminal so you can drive it normally, and snapshot the pyte-rendered grid on each quiet
moment. Launch comes from nelix.toml (NELIX_CONFIG / NELIX_EXECUTOR) — nothing hardcoded.

Usage:  .venv/bin/python spikes/spike_b_pty_pyte.py
Drive the CLI as usual; exit it normally (its /exit, or Ctrl-D at its prompt) to finish.
Frames are written to spikes/frames/NNN.txt. Use a terminal window of at least 120x40 so
the mirror lines up (the captured grid is always 120x40).
"""
import os
import select
import sys
import termios
import time
import tty
from pathlib import Path

# Make the repo root importable when run directly as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyte
import ptyprocess

from daemon.config import load_executors

COLS, ROWS = 120, 40


def main():
    execs = load_executors(os.environ.get("NELIX_CONFIG", "nelix.toml"))
    name = os.environ.get("NELIX_EXECUTOR") or next(iter(execs))
    spec = execs[name]
    os.makedirs(spec.resolved_cwd(), exist_ok=True)

    out = Path("spikes/frames")
    out.mkdir(parents=True, exist_ok=True)
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.ByteStream(screen)

    print(f"[spike_b] launching executor '{name}' over a {COLS}x{ROWS} PTY.")
    print("[spike_b] drive it normally; exit the CLI (its /exit or Ctrl-D) to finish.")
    print(f"[spike_b] frames -> {out}/NNN.txt\n")

    child = ptyprocess.PtyProcess.spawn(
        spec.argv(), dimensions=(ROWS, COLS),
        cwd=spec.resolved_cwd(), env=spec.resolved_env(),
    )
    stdin_fd = sys.stdin.fileno()
    old = termios.tcgetattr(stdin_fd)
    tty.setraw(stdin_fd)
    n, last = 0, time.time()
    try:
        while child.isalive():
            r, _, _ = select.select([child.fd, stdin_fd], [], [], 0.1)
            if child.fd in r:
                try:
                    data = child.read(65536)
                except EOFError:
                    break
                stream.feed(data)
                os.write(sys.stdout.fileno(), data)  # mirror to your terminal
                last = time.time()
            if stdin_fd in r:
                user = os.read(stdin_fd, 1024)
                if user:
                    child.write(user)               # forward your keystrokes
            if time.time() - last > 0.4:
                (out / f"{n:03d}.txt").write_text("\n".join(screen.display))
                n += 1
                last = time.time()
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old)
        child.close(force=True)
        sys.stdout.write(f"\r\n[spike_b] captured {n} frames in {out}/\r\n")


if __name__ == "__main__":
    main()
