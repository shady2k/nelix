"""Orphan reaping across daemon restart. All process inspection/killing goes through the
ProcessInspector/ProcessKiller boundary so unit tests inject a fake process table instead
of faking /proc, ps, PID reuse, or ppid==1."""
import json
import os
import signal as _signal
import subprocess
import sys
import time

import paths


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


def record_child(session_dir, record: dict) -> None:
    """Durably publish the reaping record inside the session dir (atomic temp+rename;
    fsync file and dir). Must be called AFTER spawn returns a pid/pgid and BEFORE the
    monitor thread does any work."""
    paths.ensure_private_dir(session_dir)
    final = paths.child_record(session_dir)
    tmp = final.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(record).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, final)
    dfd = os.open(session_dir, os.O_RDONLY)
    try:
        os.fsync(dfd)                  # persist the rename's directory entry
    except OSError:
        pass
    finally:
        os.close(dfd)


def read_child(session_dir):
    """Parse the record. None if absent. Unparseable -> quarantine to child.json.bad, None."""
    path = paths.child_record(session_dir)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        return json.loads(text)
    except ValueError:
        try:
            os.replace(path, str(path) + ".bad")
        except OSError:
            pass
        return None


def forget_child(session_dir) -> None:
    try:
        os.unlink(paths.child_record(session_dir))
    except FileNotFoundError:
        pass
    except OSError:
        pass


def kill_group(inspector, killer, leader_pid, pgid, grace, poll=0.1) -> bool:
    """SIGTERM the group, wait up to `grace` for the leader to die, then SIGKILL. Never
    falls back to killing by pid when pgid is missing (could hit a reused pgid) -> no-op."""
    if pgid is None:
        return False
    killer.killpg(pgid, _signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not inspector.is_alive(leader_pid):
            return True
        time.sleep(poll)
    if inspector.is_alive(leader_pid):
        killer.killpg(pgid, _signal.SIGKILL)
    return True
