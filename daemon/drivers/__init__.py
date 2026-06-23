DRIVERS = {}


def register(name):
    def deco(cls):
        DRIVERS[name] = cls
        return cls
    return deco


def get_driver(name):
    try:
        return DRIVERS[name]()
    except KeyError:
        raise ValueError(f"unknown driver: {name!r} (known: {sorted(DRIVERS)})")


# Import driver modules so their @register runs. Adding a CLI = new
# drivers/<name>.py with @register(...) plus one import line here.
from daemon.drivers import claude  # noqa: E402,F401  (registers "claude")
