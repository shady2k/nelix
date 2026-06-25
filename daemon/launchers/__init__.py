from daemon.launchers.local import LocalLauncher
from launcher_resolve import resolve_launcher

LAUNCHERS = {"local": LocalLauncher}


def get_launcher(name):
    # "auto" (the ExecutorSpec default) means "pick the launcher for this backend" — resolve it to a
    # concrete launcher (local in the MVP; a non-local backend fails closed in resolve_launcher).
    # Explicit names are taken as-is, so a genuinely unknown launcher still surfaces a clear error.
    if name == "auto":
        name = resolve_launcher(name)
    try:
        return LAUNCHERS[name]()
    except KeyError:
        raise ValueError(
            f"unknown launcher: {name!r} (known: {sorted(LAUNCHERS)}). "
            "docker/ssh launchers are post-MVP."
        )
