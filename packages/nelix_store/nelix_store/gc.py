"""GC for durable rows + runtime dirs of retired generations (nelix-80e §3.7).

A `sessions` row is a DURABLE ARCHIVED record (holds task/cwd for restart, board
reads) — it is DELETED ONLY BY THIS MODULE, NEVER at remove-live-session. If future
code deletes a sessions row at remove-live-session, the ``terminal → sessions``
RESTRICT FK will correctly refuse.
"""
import shutil

import paths
from daemon import singleton
from nelix_contracts.retirement import generation_retirement_oracle_blockers


def gc_runtime_dirs(store) -> dict:
    """Delete runtime dirs whose build_id is no longer referenced by any non-retired
    generation and whose last referencing generation is confirmed retired.

    Respects ``paths.runtime_install_lock()``: if an install is in progress the
    directories are left alone to avoid a race.

    Returns dict with ``dirs_deleted`` (count) and ``dirs_skipped`` (list of build_ids
    skipped because an install lock was held).
    """
    root = paths.runtimes_root()
    if not root.is_dir():
        return {"dirs_deleted": 0, "dirs_skipped": []}

    gens = store.list_generations()
    build_refs = {}
    for gen in gens:
        if gen.build_id is not None:
            build_refs.setdefault(gen.build_id, []).append(gen)

    if singleton.read_holder(paths.runtime_install_lock()) is not None:
        skipped = sorted(d.name for d in root.iterdir() if d.is_dir())
        return {"dirs_deleted": 0, "dirs_skipped": skipped}

    deleted = 0
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        build_id = d.name
        refs = build_refs.get(build_id, [])
        any_non_retired = any(r.lifecycle_state != "retired" for r in refs)
        if any_non_retired:
            continue
        if not refs:
            continue
        all_retired_confirmed = True
        for gen in refs:
            blockers = generation_retirement_oracle_blockers(
                store=store, generation_id=gen.generation_id)
            if blockers:
                all_retired_confirmed = False
                break
        if not all_retired_confirmed:
            continue
        shutil.rmtree(d, ignore_errors=True)
        deleted += 1

    return {"dirs_deleted": deleted, "dirs_skipped": []}
