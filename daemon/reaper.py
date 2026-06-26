"""Orphan reaping across daemon restart. All process inspection/killing goes through the
ProcessInspector/ProcessKiller boundary so unit tests inject a fake process table instead
of faking /proc, ps, PID reuse, or ppid==1."""
import os
import signal as _signal
import subprocess
import sys


class ProcessInspector:
    """Live process facts. start_fingerprint is a pid-reuse-proof identity: a process's
    start time (immutable for its lifetime), so a reused pid yields a different fingerprint."""

    def is_alive(self, pid) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True               # exists, not ours to signal
        except OSError:
            return False

    def pgid(self, pid):
        try:
            return os.getpgid(pid)
        except OSError:
            return None

    def ppid(self, pid):
        if sys.platform == "linux":
            try:
                with open(f"/proc/{pid}/stat") as f:
                    data = f.read()
                # field 4 is ppid; the comm field (2) may contain spaces/parens -> split after ')'.
                return int(data[data.rindex(")") + 2:].split()[1])
            except (OSError, ValueError):
                return None
        return self._ppid_ps(pid)

    def start_fingerprint(self, pid):
        if sys.platform == "linux":
            try:
                with open(f"/proc/{pid}/stat") as f:
                    data = f.read()
                return data[data.rindex(")") + 2:].split()[19]   # field 22 starttime (ticks)
            except (OSError, ValueError):
                return None
        return self._lstart_ps(pid)

    # ---- macOS / non-linux fallbacks via ps (same uid only; stdlib subprocess) ----
    def _ps_field(self, pid, fmt):
        try:
            out = subprocess.run(["ps", "-o", fmt, "-p", str(pid)],
                                 capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                 timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
        return lines[-1] if len(lines) >= 2 else None     # header + value

    def _ppid_ps(self, pid):
        v = self._ps_field(pid, "ppid")
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None

    def _lstart_ps(self, pid):
        return self._ps_field(pid, "lstart")             # e.g. "Wed Jun 25 12:01:02 2026"


class ProcessKiller:
    def killpg(self, pgid, sig) -> None:
        try:
            os.killpg(pgid, sig)
        except OSError:
            pass                       # already gone / not ours: best-effort
