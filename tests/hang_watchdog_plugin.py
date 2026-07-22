"""Session-wide hang watchdog: registers faulthandler for SIGUSR1 at import time
and writes per-process heartbeat + dump files for an external wrapper to consume.

Imported by conftest.py; activates only when NELIX_HANG_WATCHDOG_DIR is set.
The env var is injected by tools/hang_watchdog.py, the external wrapper that
monitors the heartbeat and sends SIGUSR1 when progress stalls.

A conftest-only watchdog is NOT enough: this module lives inside the process that
may be stuck, and the controller cannot dump its xdist workers' stacks.  The
wrapper (Part 2) handles that — this module is the in-process half that arms the
handler, writes heartbeats, and keeps the dump file open for the handler to write
into at any point, including during atexit / interpreter shutdown.

Design decisions that look odd but are intentional:

- faulthandler registered at IMPORT time, not in a hook:
  A wedged session may never reach the hook, and a wedged teardown runs AFTER
  hooks complete.  Import time is the earliest safe point and covers every phase.

- Never unregister in pytest_unconfigure:
  The handler must still work during atexit and interpreter shutdown, which is
  precisely when we are stuck.  Unregistering would disarm the one instrument
  that speaks after the test framework releases control.

- Dump file kept open for the process lifetime:
  faulthandler holds a reference to the file object.  If it is garbage-collected
  or closed the handler writes nowhere — and SIGUSR1 produces nothing.

- Individual PID files instead of a single registry:
  The wrapper reads the directory to discover live processes.  A single file
  would need locking and is harder to keep consistent across workers."""

import faulthandler
import json
import os
import signal
import time

# ---------------------------------------------------------------------------
# Activation guard: the plugin is INERT unless the wrapper sets this env var.
# Running pytest directly stays completely unaffected — zero cost, zero side
# effects.
# ---------------------------------------------------------------------------
_WATCHDOG_DIR = os.environ.get("NELIX_HANG_WATCHDOG_DIR")

if _WATCHDOG_DIR is None:
    # Inert no-op so conftest can call heartbeat() unconditionally.
    def heartbeat(phase: str) -> None:
        pass

else:
    # --- import-time registration ------------------------------------------

    _ROLE = os.environ.get("PYTEST_XDIST_WORKER", "controller")

    # Per-process append-only dump file.  Must stay open for the lifetime of
    # this process so faulthandler always has somewhere to write when SIGUSR1
    # arrives — which can happen during atexit or interpreter shutdown.
    _DUMP_PATH = os.path.join(_WATCHDOG_DIR, f"dump.{os.getpid()}")
    _DUMP_FILE = open(_DUMP_PATH, "a")  # noqa: SIM115 — intentionally kept open

    faulthandler.register(
        signal.SIGUSR1,
        all_threads=True,
        chain=False,
        file=_DUMP_FILE,
    )

    # Register this PID so the external wrapper can discover it.
    _REG_PATH = os.path.join(_WATCHDOG_DIR, f"pid.{os.getpid()}")
    with open(_REG_PATH, "w") as f:
        json.dump({"role": _ROLE, "pid": os.getpid()}, f)

    # --- heartbeat infrastructure ------------------------------------------

    # Per-process heartbeat file so concurrent xdist workers never race on a
    # shared file.  The wrapper reads all heartbeat.*.json files and uses the
    # most recent timestamp to decide whether progress has stalled.
    _HEARTBEAT_PATH = os.path.join(
        _WATCHDOG_DIR, f"heartbeat.{os.getpid()}.json"
    )

    def _write_heartbeat(phase: str) -> None:
        """Write this process's current progress marker.

        Direct write, no atomic rename — and deliberately so, but NOT because
        the write is atomic. It isn't: PIPE_BUF atomicity is a guarantee about
        pipes, not regular files, and this open(..., "w") truncates before it
        writes, so a reader can catch the file empty or half-written.

        That is survivable here because the reader is built for it. The wrapper
        parses each heartbeat.<PID>.json inside its own try and skips one that
        fails to decode, then takes the max timestamp across all the others. A
        torn read therefore costs one sample from one process, never a false
        'no progress' verdict — and the next write is
        milliseconds away. Paying for a rename per test event would buy nothing."""
        payload = {"ts": time.time(), "phase": phase}
        with open(_HEARTBEAT_PATH, "w") as f:
            json.dump(payload, f)

    # Seed the heartbeat immediately so the wrapper has a baseline timestamp
    # before the first test starts.
    _write_heartbeat("import")

    # --- public API for conftest hooks -------------------------------------

    def heartbeat(phase: str) -> None:
        """Call from pytest hooks to update the progress heartbeat.

        The wrapper reads heartbeat.json and considers the run 'stopped' if the
        timestamp is older than its no-progress threshold."""
        _write_heartbeat(phase)
