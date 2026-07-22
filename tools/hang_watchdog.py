#!/usr/bin/env python3
"""External hang-watchdog wrapper for pytest: monitors a progress heartbeat and
forces thread-stack dumps when progress stalls, then terminates the run if it
remains stuck so the CI workflow's `if: failure()` steps fire.

Stdlib only — no new dependencies in requirements.txt.

The wrapper creates a registry directory, exports it to pytest via an env var,
and starts the suite with start_new_session=True (new process group).  It then
polls the heartbeat file.  If progress stops for too long, it sends SIGUSR1 to
every registered process INDIVIDUALLY (not killpg — the process group also holds
broker subprocesses and test children that have no handler and would simply die),
copies their dump files straight to stderr (bypassing xdist capture and execnet
buffering), and — if the suite is still alive at the hard timeout — terminates the
process group and exits non-zero.

Thresholds:
  --no-progress N   seconds without a heartbeat update before first dump (default 600)
  --hard-timeout N  total seconds before forced termination (default 900)
  --kill-grace N    seconds between SIGTERM and SIGKILL (default 30)
  --dump-drain N    seconds to wait after SIGUSR1 before reading dump files (default 5)

An outer timeout (CI's timeout-minutes: 30) remains as a backstop for any case
where even this wrapper is stuck.  The wrapper's hard timeout is deliberately
inside the CI cap so the forced termination runs before GitHub cancels the job.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def _read_heartbeat(watchdog_dir: str) -> dict | None:
    """Return the most recent heartbeat payload across all processes.

    Each process writes its own heartbeat.<PID>.json to avoid races.
    We scan all of them and return the one with the highest timestamp."""
    best = None
    try:
        for name in os.listdir(watchdog_dir):
            if not name.startswith("heartbeat."):
                continue
            path = os.path.join(watchdog_dir, name)
            try:
                with open(path) as f:
                    hb = json.load(f)
                if best is None or hb.get("ts", 0) > best.get("ts", 0):
                    best = hb
            except (json.JSONDecodeError, OSError):
                pass
    except FileNotFoundError:
        pass
    return best


def _registered_pids(watchdog_dir: str) -> list[dict]:
    """Return [{pid, role}, ...] for every process that registered.

    Each process writes pid.<PID> at import time.  We read all of them to
    discover live processes without relying on /proc or ps — the presence of a
    pid file does NOT guarantee the process is still alive, but sending a signal
    to a dead PID is harmless (it simply fails silently in kill())."""
    pids = []
    try:
        for name in os.listdir(watchdog_dir):
            if not name.startswith("pid."):
                continue
            path = os.path.join(watchdog_dir, name)
            try:
                with open(path) as f:
                    pids.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                pass
    except FileNotFoundError:
        pass
    return pids


def _dump_stacks(watchdog_dir: str, drain: int = 5) -> None:
    """Send SIGUSR1 to every registered PID individually.

    NOT killpg: the process group includes broker subprocesses and test children
    that did NOT register a faulthandler — sending them SIGUSR1 would kill them
    (default action is terminate), compounding the hang instead of diagnosing it.

    After sending the signals, wait for the dump files to be written, then copy
    each one to stderr so the output bypasses xdist capture and execnet buffering
    and lands directly in the CI log."""
    pids = _registered_pids(watchdog_dir)
    if not pids:
        print("hang_watchdog: no registered PIDs to dump", file=sys.stderr)
        return

    print(
        f"hang_watchdog: sending SIGUSR1 to {len(pids)} process(es)...",
        file=sys.stderr,
    )
    for entry in pids:
        pid = entry["pid"]
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError:
            pass  # process already gone

    # Let faulthandler write the dumps.
    time.sleep(drain)

    # Copy each dump file to stderr.
    for entry in pids:
        pid = entry["pid"]
        role = entry.get("role", "unknown")
        dump_path = os.path.join(watchdog_dir, f"dump.{pid}")
        try:
            with open(dump_path) as f:
                content = f.read()
            if content.strip():
                print(
                    f"\n===== FAULTHANDLER DUMP — {role} (pid {pid}) "
                    f"=====",
                    file=sys.stderr,
                )
                sys.stderr.write(content)
                sys.stderr.flush()
        except FileNotFoundError:
            print(
                f"hang_watchdog: no dump file for {role} (pid {pid})",
                file=sys.stderr,
            )


def _terminate_group(proc: subprocess.Popen, grace: int) -> None:
    """Terminate the process group, then SIGKILL after a short grace.

    We own the process group (start_new_session=True), so os.killpg targets
    exactly the pytest tree and nothing else."""
    print(
        "hang_watchdog: sending SIGTERM to process group...",
        file=sys.stderr,
    )
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        pass

    deadline = time.time() + grace
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(1)

    print(
        "hang_watchdog: process group still alive; sending SIGKILL...",
        file=sys.stderr,
    )
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        pass


def _process_tree(pids: list[dict]) -> None:
    """Print a human-readable process tree using only stdlib.

    On Linux we read /proc; on macOS we shell out to `ps`.  This is a
    best-effort diagnostic — if it fails we continue on."""
    print(
        "\nhang_watchdog: registered processes:",
        file=sys.stderr,
    )
    for entry in pids:
        print(
            f"  pid={entry['pid']} role={entry.get('role', '?')}",
            file=sys.stderr,
        )

    # Try to print a richer tree if the platform supports it.
    if sys.platform == "linux":
        try:
            subprocess.run(
                ["ps", "f", "-g", str(os.getpid())],
                stderr=subprocess.DEVNULL,
                stdout=sys.stderr,
            )
        except Exception:
            pass
    else:
        try:
            subprocess.run(
                ["ps", "-o", "pid,ppid,pgid,command"],
                stderr=subprocess.DEVNULL,
                stdout=sys.stderr,
            )
        except Exception:
            pass


def _pytest_cmd() -> list[str]:
    """Return the pytest command to run.

    Uses ./.venv if it exists (local dev / make test), otherwise falls back to
    the bare `pytest` on PATH (CI runner — the workflow installs into the
    system Python)."""
    venv_pytest = os.path.join(".venv", "bin", "pytest")
    if os.path.isfile(venv_pytest):
        return [venv_pytest, "-q"]
    return ["pytest", "-q"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run pytest with a hang watchdog that forces stack dumps "
                    "and terminates wedged runs so CI failure() steps fire."
    )
    ap.add_argument(
        "--no-progress", type=int, default=600,
        help="Seconds without heartbeat before first dump (default: 600 = 10 min)",
    )
    ap.add_argument(
        "--hard-timeout", type=int, default=900,
        help="Seconds total before forced termination (default: 900 = 15 min)",
    )
    ap.add_argument(
        "--kill-grace", type=int, default=30,
        help="Seconds between SIGTERM and SIGKILL (default: 30)",
    )
    ap.add_argument(
        "--dump-drain", type=int, default=5,
        help="Seconds to wait after SIGUSR1 before reading dumps (default: 5)",
    )
    ap.add_argument(
        "pytest_args", nargs=argparse.REMAINDER,
        help="Arguments forwarded to pytest",
    )
    args = ap.parse_args(argv)

    # argparse.REMAINDER captures '--' itself as the first element of
    # pytest_args when the caller uses '--' to separate wrapper flags from
    # pytest args.  Strip it so it doesn't leak into the pytest command.
    pytest_args = args.pytest_args
    if pytest_args and pytest_args[0] == "--":
        pytest_args = pytest_args[1:]

    watchdog_dir = tempfile.mkdtemp(prefix="nelix-hang-watchdog-")

    env = os.environ.copy()
    env["NELIX_HANG_WATCHDOG_DIR"] = watchdog_dir

    cmd = _pytest_cmd() + pytest_args

    print(
        f"hang_watchdog: starting pytest (no-progress={args.no_progress}s, "
        f"hard-timeout={args.hard_timeout}s, dir={watchdog_dir})",
        file=sys.stderr,
    )

    proc = subprocess.Popen(
        cmd,
        env=env,
        start_new_session=True,
        # stdout/stderr inherit so output streams directly to the CI log.
    )

    # Use time.time() (wall clock), not time.monotonic(): the plugin writes
    # time.time() timestamps into heartbeat files, and comparing clocks that
    # have different epochs guarantees the comparison is always wrong.
    start_time = time.time()
    last_progress = start_time
    already_dumped = False
    already_force_dumped = False

    while proc.poll() is None:
        time.sleep(1)  # poll at 1 Hz; the thresholds are in minutes

        now = time.time()
        elapsed_total = now - start_time
        elapsed_no_progress = now - last_progress

        # Update last_progress from the heartbeat file.
        hb = _read_heartbeat(watchdog_dir)
        if hb is not None and hb.get("ts", 0) > last_progress:
            last_progress = hb["ts"]
            # Progress was made since the last dump — reset the dump gate so a
            # second stall triggers a second dump.
            if already_dumped:
                already_dumped = False

        # --- First dump: no progress for too long --------------------------
        if elapsed_no_progress >= args.no_progress and not already_dumped:
            print(
                f"\nhang_watchdog: NO PROGRESS for {elapsed_no_progress:.0f}s "
                f"(threshold {args.no_progress}s). Dumping stacks...",
                file=sys.stderr,
            )
            hb = _read_heartbeat(watchdog_dir)
            if hb:
                print(
                    f"hang_watchdog: last heartbeat phase = {hb.get('phase', '?')} "
                    f"at t={hb.get('ts', 0) - start_time:.0f}s",
                    file=sys.stderr,
                )
            pids = _registered_pids(watchdog_dir)
            _process_tree(pids)
            _dump_stacks(watchdog_dir, args.dump_drain)
            already_dumped = True

        # --- Hard timeout: dump again, then terminate ----------------------
        if elapsed_total >= args.hard_timeout and not already_force_dumped:
            print(
                f"\nhang_watchdog: HARD TIMEOUT at {elapsed_total:.0f}s "
                f"(threshold {args.hard_timeout}s). Forcing termination...",
                file=sys.stderr,
            )
            _dump_stacks(watchdog_dir, args.dump_drain)
            _terminate_group(proc, args.kill_grace)
            already_force_dumped = True

    # --- pytest exited on its own ------------------------------------------
    exit_code = proc.returncode

    # If WE forced termination, the exit code is our failure signal — not
    # whatever the dying process returned.  This is the whole point of the
    # wrapper: a non-zero exit makes the workflow's `if: failure()` steps run.
    if already_force_dumped:
        exit_code = 1

    # Clean up the registry directory.
    shutil.rmtree(watchdog_dir, ignore_errors=True)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
