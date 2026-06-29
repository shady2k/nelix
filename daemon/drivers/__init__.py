DRIVERS = {}


def register(name):
    def deco(cls):
        DRIVERS[name] = cls
        return cls
    return deco


# The actuation/observe members every driver must implement (the rich observe() contract,
# spec §5.6). The registry FAILS CLOSED for an un-migrated driver rather than letting the core
# call a missing classification contract at runtime.
_REQUIRED_MEMBERS = ("observe", "normalize_frame", "is_transcript_volatile",
                     "format_submission", "submit_text", "select_option", "interrupt")


def get_driver(name):
    try:
        cls = DRIVERS[name]
    except KeyError:
        raise ValueError(f"unknown driver: {name!r} (known: {sorted(DRIVERS)})")
    missing = [m for m in _REQUIRED_MEMBERS if not callable(getattr(cls, m, None))]
    if missing:
        raise TypeError(f"driver {name!r} does not implement the observe() contract: "
                        f"missing {missing} — register a fully-migrated driver (spec §5.6)")
    return cls()


# Import driver modules so their @register runs. Adding a CLI = new
# drivers/<name>.py with @register(...) plus one import line here.
from daemon.drivers import claude  # noqa: E402,F401  (registers "claude")
