import os
import tomllib
from dataclasses import dataclass


@dataclass
class ExecutorSpec:
    command: str
    args: list
    env: dict
    driver: str
    launcher: str = "auto"
    settle_seconds: float = 1.5
    delivery_confirm_seconds: float = 10.0   # how long to wait for delivery confirmation before failing
    respond_write_seconds: float = 5.0       # deadline for the respond() PTY write (wedged-stdin guard)
    max_idle_seconds: float = 600.0      # recovery: no-progress watchdog (daemon); 0 = disabled
    max_restarts: int = 3                # recovery: consecutive auto-restarts before escalating (Hermes)
    tail_lines: int = 400
    status_tail_chars: int = 4000
    dialog_page_chars: int = 8000
    spool_max_bytes: int = 8_388_608

    def argv(self):
        return [self.command, *self.args]

    def resolved_env(self):
        merged = dict(os.environ)
        for k, v in self.env.items():
            merged[k] = os.path.expanduser(str(v))
        return merged


def _spec_num(spec, key, default, *, cast, floor=0):
    """Per-executor numeric tunable: non-numeric / bool / below-floor -> default
    (don't crash the daemon load on a hand-edited typo)."""
    v = spec.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < floor:
        return default
    return cast(v)


def load_executors(path):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    out = {}
    for name, spec in data.get("executors", {}).items():
        if "driver" not in spec:
            raise ValueError(f"executor {name!r}: 'driver' is required")
        out[name] = ExecutorSpec(
            command=spec["command"],
            args=list(spec.get("args", [])),
            env=dict(spec.get("env", {})),
            driver=spec["driver"],
            launcher=spec.get("launcher", "auto"),
            settle_seconds=float(spec.get("settle_seconds", 1.5)),
            delivery_confirm_seconds=_spec_num(spec, "delivery_confirm_seconds", 10.0, cast=float),
            respond_write_seconds=_spec_num(spec, "respond_write_seconds", 5.0, cast=float),
            max_idle_seconds=_spec_num(spec, "max_idle_seconds", 600.0, cast=float),
            max_restarts=_spec_num(spec, "max_restarts", 3, cast=int),
            tail_lines=int(spec.get("tail_lines", 400)),
            status_tail_chars=int(spec.get("status_tail_chars", 4000)),
            dialog_page_chars=int(spec.get("dialog_page_chars", 8000)),
            spool_max_bytes=int(spec.get("spool_max_bytes", 8_388_608)),
        )
    return out


def load_concurrency_limit(path, default=1):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return int(data.get("concurrency_limit", default))


@dataclass
class RetentionConfig:
    daemon_log_retain: int = 10
    session_retain: int = 20
    session_max_age_days: int = 7


def _cfg_int(data, key, default, floor):
    v = data.get(key, default)
    # Strictly int: reject bool (a subclass of int), float (e.g. 1.9), str, etc. -> default.
    if isinstance(v, bool) or not isinstance(v, int):
        return default
    return v if v >= floor else default


def load_retention(path):
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        data = {}
    return RetentionConfig(
        daemon_log_retain=_cfg_int(data, "daemon_log_retain", 10, floor=1),
        session_retain=_cfg_int(data, "session_retain", 20, floor=0),
        session_max_age_days=_cfg_int(data, "session_max_age_days", 7, floor=0),
    )


_VALID_LEVELS = ("debug", "info", "warning", "error")


@dataclass
class LogLevelConfig:
    level: str
    invalid_value: "str | None" = None
    invalid_source: "str | None" = None


def _norm_level(s):
    if isinstance(s, str) and s.strip().lower() in _VALID_LEVELS:
        return s.strip().lower()
    return None


def _file_log_level_raw(path):
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return None
    v = data.get("log_level")
    return v if isinstance(v, str) else None   # missing / non-str -> treat as unset


def load_log_level(path, default="info"):
    env_raw = os.environ.get("NELIX_LOG_LEVEL")
    file_raw = _file_log_level_raw(path)
    file_ok = _norm_level(file_raw)
    if env_raw is not None:
        env_ok = _norm_level(env_raw)
        if env_ok:
            return LogLevelConfig(level=env_ok)
        return LogLevelConfig(level=file_ok or default,
                              invalid_value=env_raw, invalid_source="env")
    if file_raw is None:
        return LogLevelConfig(level=default)
    if file_ok:
        return LogLevelConfig(level=file_ok)
    return LogLevelConfig(level=default, invalid_value=file_raw, invalid_source="file")
