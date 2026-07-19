"""Mutation sweep for the owner_id guards (nelix-9a4.3): break each one, prove a test goes RED.

Run: python tests/tools/owner_mutation_sweep.py   (not a pytest test — it EDITS daemon/ in place
and restores it, so it must never run inside the suite it is mutating.)

A guard with no failing test is decoration. For each mutation we apply a single edit that
disables one guard, run the owner detector suite, and require it to FAIL. Any mutation that
survives (suite still green) is reported as a HOLE.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]   # repo root, wherever it is checked out
DETECTORS = ["tests/test_owner_isolation.py", "tests/test_owner.py",
             "tests/test_owner_contract_drift.py", "tests/test_nelix_wait.py"]

MUTANTS = [
    ("manager: _owned always finds the session (drop the gate)",
     "daemon/manager.py",
     "        if not owner.owns_session(session_id, owner_id):\n            return None\n        with self._lock:\n            return self._sessions.get(session_id)",
     "        with self._lock:\n            return self._sessions.get(session_id)"),

    ("manager: board listing stops filtering by owner",
     "daemon/manager.py",
     "            if sid not in mine:\n                continue",
     "            pass"),

    ("manager: terminal inventory stops filtering by owner",
     "daemon/manager.py",
     '        recent = {sid: snap for sid, snap in recent_all.items()\n                  if owner.owns_session(sid, owner_id)}',
     '        recent = dict(recent_all)'),

    ("manager: respond drops its owner check",
     "daemon/manager.py",
     '        if not owner.owns_session(session_id, owner_id):\n            return RespondOutcome("unknown_session")',
     "        pass"),

    ("manager: stop drops its owner check",
     "daemon/manager.py",
     '        if not owner.owns_session(session_id, owner_id):\n            return StopOutcome("unknown_session")\n        return self._stop(session_id, reason=reason)',
     "        return self._stop(session_id, reason=reason)"),

    ("manager: restart drops its owner gate (any caller may restart any session)",
     "daemon/manager.py",
     "        stored_owner = owner.session_owned_by(session_id, owner_id)\n        if stored_owner is None:      # not ours, or no trustworthy record -> fail closed\n            return None",
     "        stored_owner = owner.owner_of(paths.sessions_root() / session_id)"),

    # THE skeleton key: spell the ownership decision as a bool-returning raw ==, which is how it
    # would naturally be written and which grants an ownerless session to an owner-less caller.
    ("owner: owns() spelled as a raw == (None == None skeleton key)",
     "daemon/owner.py",
     "    return owned_by(session_dir, owner_id) is not None",
     "    return owner_of(session_dir) == owner_id"),

    ("manager: start does not persist the owner record",
     "daemon/manager.py",
     "            owner.write(paths.sessions_root() / sid, owner_id)\n            sess.start(task, cwd)",
     "            sess.start(task, cwd)"),

    ("manager: start does not shape-check the owner (bad owner -> 409, not 400)",
     "daemon/manager.py",
     "            owner.validate(owner_id)\n            spec = self._specs.get(executor_name)",
     "            spec = self._specs.get(executor_name)"),

    ("rpc: /wait arms without checking ownership",
     "daemon/rpc_server.py",
     '                if not owner.owns_session(sid, owner_id):\n                    self._send(404, {"error": "unknown session",\n                                     "hint": "unknown session, or not this owner\'s; a wait on it"\n                                             " would never wake. Do not retry."})\n                    return',
     "                pass"),

    ("rpc: /wait answers an un-armable wait with 200/null (retry storm)",
     "daemon/rpc_server.py",
     '                    self._send(404, {"error": "unknown session",\n                                     "hint": "unknown session, or not this owner\'s; a wait on it"\n                                             " would never wake. Do not retry."})\n                    return',
     '                    self._send(200, {"event": None}); return'),

    ("rpc: /dialog reads the transcript without checking ownership",
     "daemon/rpc_server.py",
     '                if not owner.owns_session(sid, owner_id):\n                    self._send(404, {"error": "unknown session",\n                                     "hint": "the session may have exited or not started;"\n                                             " call nelix_status (no session_id) to list sessions."})\n                    return',
     "                pass"),

    ("rpc: owner_id becomes optional (missing -> unfiltered wildcard)",
     "daemon/rpc_server.py",
     '            if val is None:\n                raise _BadRequest(400, "missing owner_id")',
     "            if val is None:\n                return None"),

    ("owner: owner_of() trusts a malformed stored owner_id",
     "daemon/owner.py",
     '    try:\n        return validate(rec.get("owner_id"))\n    except OwnerRejected:                 # a record we cannot trust is a record we do not honour\n        return None',
     '    return rec.get("owner_id")'),

    ("owner: write() is best-effort (swallows OSError like the capture sidecar)",
     "daemon/owner.py",
     '        raise OwnerWriteFailed(f"could not persist owner record for {session_dir}: {e}") from e',
     "        return"),
]


def run(paths_):
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", *paths_],
                       cwd=ROOT, capture_output=True, text=True)
    return r.returncode, (r.stdout.strip().splitlines() or ["<no output>"])[-1]


def main():
    rc, line = run(DETECTORS)
    if rc != 0:
        print(f"ABORT: detector suite is not green to begin with: {line}")
        return 1
    print(f"baseline detectors: {line}\n")

    holes = []
    for name, relpath, old, new in MUTANTS:
        p = ROOT / relpath
        src = p.read_text()
        if old not in src:
            print(f"  SKIP (pattern not found)  {name}")
            holes.append((name, "PATTERN NOT FOUND — mutation not applied"))
            continue
        p.write_text(src.replace(old, new, 1))
        try:
            rc, line = run(DETECTORS)
        finally:
            p.write_text(src)
        if rc == 0:
            print(f"  SURVIVED  {name}\n            -> {line}")
            holes.append((name, line))
        else:
            line.split(" - ")[0]
            print(f"  caught    {name}")

    print()
    if holes:
        print(f"{len(holes)} MUTANT(S) SURVIVED — those guards have no test:")
        for n, l in holes:
            print(f"  - {n}: {l}")
        return 1
    print(f"all {len(MUTANTS)} mutants caught: every guard has a test that fails without it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
