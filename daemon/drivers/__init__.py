from daemon.drivers.claude import ClaudeDriver

DRIVERS = {"claude": ClaudeDriver}


def get_driver(name):
    try:
        return DRIVERS[name]()
    except KeyError:
        raise ValueError(f"unknown driver: {name!r} (known: {sorted(DRIVERS)})")
