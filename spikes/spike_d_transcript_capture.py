"""Spike D — does live scroll-off capture yield a coherent transcript? (gates Task 4)

Captures a REAL executor's session two ways and lets you eyeball whether the pyte
HistoryScreen reconstructs the dialog above the final viewport:
  1. raw  — every PTY byte (forensic ground truth)            -> spikes/_spike_d_raw
  2. transcript — scrolled-off history lines + final viewport -> spikes/_spike_d_transcript.txt

Run under a Vault-capable shell (envconsul needs Vault auth), from the repo root:

  NELIX_EXECUTOR_ARGV='envconsul -secret=homelab/zai -no-prefix -once \
    -vault-renew-token=false -- /Users/shady/Documents/scripts/zai-wrapper.sh' \
  NELIX_EXECUTOR_CWD=~/tmp/nelix-skeleton \
  .venv/bin/python spikes/spike_d_transcript_capture.py "create test.txt with the word nelix"

ACCEPTANCE (record the verdict in spikes/transcript_capture_result.md):
  PASS  -> the transcript is readable, prose above the final viewport is preserved,
           no duplicated repaint frames. Task 4 uses the HistoryScreen scroll-off line-source.
  FAIL  -> history empty/garbled (repaint-only / alt-screen TUI). Task 4 uses the per-stop
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
    time.sleep(2.0)                      # let the TUI settle before sending the task
    child.write((task + "\r").encode())

    # select-before-read so idle periods don't block past the deadline (the executor is a
    # long-running TUI that never exits on its own — the timeout is what stops the spike).
    deadline = time.time() + timeout
    while time.time() < deadline and child.isalive():
        try:
            r, _, _ = select.select([child.fd], [], [], 0.5)
        except (OSError, ValueError):
            break
        if not r:
            continue                     # idle tick — re-check the deadline
        try:
            data = child.read(65536)
        except (EOFError, OSError):
            break
        if not data:
            continue
        raw.extend(data)
        stream.feed(data)

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
