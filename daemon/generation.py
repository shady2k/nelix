"""Derive the running generation's identity from the interpreter executing it (spec §8, §10:
`/health` as a liveness/identity probe carries `generation_id`).

An INSTALLED generation runs `~/.nelix/runtimes/<build-id>/venv/bin/python*`
(`paths.py:92 runtime_dir`, `runtime.py:141 build_id`) — so the build id is recoverable from
`sys.executable` alone, with zero new state and zero new dependency: daemon/ deliberately does not
import `runtime.py` (that module provisions/installs runtimes; it is raw material for `nelix
daemon ensure` [nelix-3rm], not something a running generation needs at runtime) or
`nelix_contracts` (see `daemon/owner.py`'s header) — this module only ever PARSES a path string.

In dev/test the daemon runs from the repo's own checkout `.venv`, which sits OUTSIDE
`paths.runtimes_root()`: there is no build id there, and `generation_id()` honestly reports None
rather than fabricate one.
"""
import sys
from pathlib import Path

import paths


def generation_id(python_path=None) -> "str | None":
    """The build-id of the generation whose interpreter is at `python_path` (default
    `sys.executable`), or None if that interpreter is not an installed runtime.

    A pure path parse — no filesystem access, nothing needs to exist — so a test can force EITHER
    branch by passing an arbitrary path, without installing a real runtime (see `runtime.py`'s
    `runtime_python`: `runtimes_root()/<build-id>/venv/bin/python`, the exact shape matched here).
    """
    python_path = Path(python_path if python_path is not None else sys.executable)
    root = paths.runtimes_root()
    try:
        rel = python_path.relative_to(root)
    except ValueError:
        return None                     # not under runtimes_root at all -> not an installed runtime
    parts = rel.parts
    # <build-id>/venv/bin/<python...> — anything else nested under runtimes_root (a manifest, a
    # differently-shaped tree, a stray file) is not a generation's interpreter and must not be
    # reported as one. `startswith("python")` covers both the bare `python` venv creates and the
    # versioned `python3.11` symlink alongside it.
    if len(parts) == 4 and parts[1] == "venv" and parts[2] == "bin" and parts[3].startswith("python"):
        return parts[0]
    return None
