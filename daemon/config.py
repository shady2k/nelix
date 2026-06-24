import os
import tomllib
from dataclasses import dataclass


@dataclass
class ExecutorSpec:
    command: str
    args: list
    env: dict
    cwd: str
    driver: str
    launcher: str = "auto"
    settle_seconds: float = 1.5
    hang_timeout: float = 600.0
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

    def resolved_cwd(self):
        return os.path.expanduser(self.cwd)


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
            cwd=spec.get("cwd", "."),
            driver=spec["driver"],
            launcher=spec.get("launcher", "auto"),
            settle_seconds=float(spec.get("settle_seconds", 1.5)),
            hang_timeout=float(spec.get("hang_timeout", 600.0)),
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
    try:
        v = int(v)
    except (TypeError, ValueError):
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
