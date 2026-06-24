"""Spike D — does live scroll-off capture yield a coherent transcript? (gates Task 4)

Captures a REAL executor's session two ways and lets you eyeball whether the pyte
HistoryScreen reconstructs the dialog above the final viewport:
  1. raw  — every PTY byte (forensic ground truth)            -> spikes/_spike_d_raw
  2. transcript — scrolled-off history lines + final viewport -> spikes/_spike_d_transcript.txt

Drives the TUI exactly like the daemon (daemon/session.py): wait until the TUI is ready,
type the task, let it render, THEN send CR separately (the TUI treats CR as Enter; sending
text+CR in one burst leaves the task unsent — the first run hit that).

Run under a Vault-capable shell (envconsul needs Vault auth), from the repo root:

  NELIX_EXECUTOR_ARGV='envconsul -secret=homelab/zai -no-prefix -once \
    -vault-renew-token=false -- /Users/shady/Documents/scripts/zai-wrapper.sh' \
  NELIX_EXECUTOR_CWD=~/tmp/nelix-skeleton \
  .venv/bin/python spikes/spike_d_transcript_capture.py "Print a numbered list from 1 to 60, one per line, then stop."

ACCEPTANCE (record the verdict in spikes/transcript_capture_result.md):
  PASS  -> transcript readable, prose above the final viewport preserved (early numbers 1,2,…
           present though they scrolled off), no duplicated repaint frames -> Task 4 uses the
           HistoryScreen scroll-off line-source.
  FAIL  -> history_lines=0 / garbled (repaint-only / alt-screen TUI) -> Task 4 uses the per-stop
           snapshot fallback (screen-height limited); update the spec's fidelity caveat.
"""
import os
import select
import shlex
import sys
import time

import pyte
import ptyprocess

COLS, ROWS = 120, 40
OUT_DIR = os.path.dirname(os.path.abspath(__file__))


def _row_text(row):
    """pyte history/buffer rows are column->Char mappings; display rows are strings."""
    if isinstance(row, str):
        return row.rstrip()
    try:
        return "".join(row[x].data for x in range(COLS)).rstrip()
    except Exception:
        return "".join(getattr(ch, "data", "") for ch in row.values()).rstrip()


def _pump_once(child, stream, raw, timeout=0.2):
    try:
        r, _, _ = select.select([child.fd], [], [], timeout)
    except (OSError, ValueError):
        return False
    if not r:
        return False
    try:
        data = child.read(65536)
    except (EOFError, OSError):
        return False
    if not data:
        return False
    raw.extend(data)
    stream.feed(data)
    return True


def _wait_until_ready(child, stream, screen, raw, stable_for=1.5, timeout=20.0):
    last = None
    stable_since = None
    deadline = time.time() + timeout
    while time.time() < deadline and child.isalive():
        _pump_once(child, stream, raw, 0.1)
        cur = "\n".join(screen.display)
        if cur != last:
            last = cur
            stable_since = time.time()
        elif cur.strip() and stable_since is not None and time.time() - stable_since >= stable_for:
            return


def main():
    argv_env = os.environ.get("NELIX_EXECUTOR_ARGV")
    if not argv_env:
        sys.exit("set NELIX_EXECUTOR_ARGV='<command + args>' (see this file's docstring)")
    argv = shlex.split(argv_env)
    cwd = os.path.expanduser(os.environ.get("NELIX_EXECUTOR_CWD", ".")) or None
    if cwd:
        os.makedirs(cwd, exist_ok=True)
    task = sys.argv[1] if len(sys.argv) > 1 else "say hello then stop"
    timeout = float(os.environ.get("NELIX_SPIKE_TIMEOUT", "60"))

    screen = pyte.HistoryScreen(COLS, ROWS, history=100000, ratio=0.5)
    stream = pyte.ByteStream(screen)
    raw = bytearray()

    child = ptyprocess.PtyProcess.spawn(argv, dimensions=(ROWS, COLS), cwd=cwd)

    # Submit the task the way the daemon does: ready -> type -> render -> CR (separately).
    _wait_until_ready(child, stream, screen, raw)
    child.write(task.encode())
    time.sleep(0.3)
    for _ in range(5):
        _pump_once(child, stream, raw, 0.1)   # let the typed text render
    child.write(b"\r")

    # Capture until timeout (the TUI is long-running; idle ticks re-check the deadline).
    deadline = time.time() + timeout
    while time.time() < deadline and child.isalive():
        _pump_once(child, stream, raw, 0.5)

    history = [_row_text(r) for r in screen.history.top]
    viewport = [_row_text(r) for r in screen.display]
    transcript = "\n".join(history + viewport)

    with open(os.path.join(OUT_DIR, "_spike_d_raw"), "wb") as f:
        f.write(bytes(raw))
    with open(os.path.join(OUT_DIR, "_spike_d_transcript.txt"), "w") as f:
        f.write(transcript)

    print(f"raw_bytes={len(raw)} history_lines={len(history)} viewport_lines={len(viewport)}")
    print("---- transcript (tail 4000) ----")
    print(transcript[-4000:])
    try:
        child.close(force=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
