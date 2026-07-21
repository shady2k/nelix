"""Reconcile `runtimes/current` against the store's active generation.

Two facts cannot be flipped in one transaction: the store's active-generation row and a symlink on
disk. So one is the truth — the store — and the other is a cache that must be repairable. A crash
between them leaves the cache stale, and router/app.py reads the CACHE to decide which build new
daemons are spawned from, which is why this runs at startup before anything else looks.

It never raises. This is on the router's startup path, and refusing to start because a cache could
not be checked is a worse outcome than starting with a stale one.
"""
import logging

import runtime

_log = logging.getLogger("nelix.router.reconcile_current")


def _authoritative_build(store):
    for gen in store.list_generations():
        if getattr(gen, "lifecycle_state", None) == "active":
            return gen.build_id
    return None


def reconcile(store, *, log=None) -> dict:
    """Point `current` at the store's active build when they disagree. Returns what it found and
    what it did; `action` is one of "none", "repaired", "unrepairable"."""
    log = log or _log
    out = {"authoritative": None, "current": None, "action": "none"}
    try:
        out["authoritative"] = _authoritative_build(store)
    except Exception as e:                       # noqa: BLE001 - a cache check must not stop a start
        log.warning("could not read the active generation; leaving runtimes/current alone: %s", e)
        return out
    try:
        out["current"] = runtime.active()
    except Exception:                            # noqa: BLE001 - absent/dangling reads as unknown
        out["current"] = None

    authoritative = out["authoritative"]
    if authoritative is None or authoritative == out["current"]:
        return out

    if not runtime.is_installed(authoritative):
        log.error("the active generation names build %s but its runtime is not installed; "
                  "runtimes/current still points at %s", authoritative, out["current"])
        out["action"] = "unrepairable"
        return out

    try:
        runtime.activate(authoritative)
    except Exception as e:                       # noqa: BLE001
        log.error("could not repair runtimes/current to %s: %s", authoritative, e)
        out["action"] = "unrepairable"
        return out

    log.warning("repaired runtimes/current: was %s, the active generation runs %s",
                out["current"], authoritative)
    out["action"] = "repaired"
    return out
