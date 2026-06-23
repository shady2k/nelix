import os


def _profile_backend() -> str:
    backend = os.getenv("TERMINAL_ENV")
    if backend:
        return backend.strip().lower()
    try:                                  # fallback: stored config value
        from hermes_cli.config import load_config_readonly, cfg_get
        return str(cfg_get(load_config_readonly(), "terminal", "backend",
                           default="local")).strip().lower()
    except Exception:
        return "local"


def resolve_launcher(configured: str, allow_weaker: bool = False) -> str:
    backend = _profile_backend()
    if configured == "auto":
        if backend == "local":
            return "local"
        raise NotImplementedError(
            f"launcher=auto resolved to backend {backend!r}; only local is implemented (post-MVP)")
    if configured == "local":
        if backend != "local" and not allow_weaker:
            raise PermissionError(
                f"executor launcher 'local' is weaker than profile backend {backend!r}; "
                "set allow_weaker_than_profile to override")
        return "local"
    raise NotImplementedError(f"launcher {configured!r} is post-MVP")
