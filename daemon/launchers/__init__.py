from daemon.launchers.local import LocalLauncher

LAUNCHERS = {"local": LocalLauncher}


def get_launcher(name):
    try:
        return LAUNCHERS[name]()
    except KeyError:
        raise ValueError(
            f"unknown launcher: {name!r} (known: {sorted(LAUNCHERS)}). "
            "docker/ssh launchers are post-MVP."
        )
