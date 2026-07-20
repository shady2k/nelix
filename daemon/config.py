import math
import os
import tomllib
from dataclasses import dataclass, field


# Message-plane caps (executor -> orchestrator async messages; consumed by daemon/messages.py and
# the /message route in daemon/rpc_server.py).
# MSG_MAX_BODY bounds the raw HTTP request body the /message route will read (a 413 past this),
# NOT the sum of the parsed-field caps below: a `question` payload carries several free-text fields
# (question/continuation_plan/assumption/impact_if_wrong, each capped at MAX_BODY_LEN) plus JSON
# overhead, which can exceed a tight per-field-sum bound — so this mirrors /hook's tight body cap
# (daemon/rpc_server.py's _HOOK_MAX_BODY) rather than trying to derive a smaller value from the
# field caps.
MSG_MAX_BODY = 256 * 1024  # max raw HTTP request body bytes accepted by the message route
MAX_PROGRESS_NOTES = 50  # max progress notes retained per session
MAX_SUMMARY_LEN = 280    # ProgressNote.summary cap (short, tweet-length)
MAX_BODY_LEN = 4000      # cap for longer free-text fields (question, continuation_plan, details)
DEFAULT_DIALOG_PAGE_CHARS = 8000  # /dialog page size when the caller omits limit (bounded page + next_offset cursor)


@dataclass
class BeliefConfig:
    """Tunables for the pure BeliefEngine (spec §7). All durations are seconds; the engine reads
    `now` from its injected clock and never sleeps. Task 13 wires liveness-scaled watchdog budgets;
    the defaults here are the engine's standalone defaults."""
    # §7.1 confirmation window for *suspicious* idle edges (not a multi-second settle).
    idle_confirm_window: float = 0.5
    # §7.1 post-submit TTFT suppression bound (cleared early by a positive turn-start signal).
    post_submit_grace: float = 8.0
    # §7.1 hard bound on echo suppression: an answer whose Enter never landed holds the echo in the
    # box forever; past this long the never-clearing box surfaces a needs-attention wake (nelix-sud).
    echo_stuck_after: float = 20.0
    # §7.2 anti-flap: don't re-mint the same semantic_fp within this cooldown after withdrawing it.
    withdrawn_cooldown: float = 1.0
    # §7.4 heartbeat frozen-but-should-tick -> stale after this long without a heartbeat fp change.
    heartbeat_stale_after: float = 10.0
    # §7.4 liveness-scaled watchdog budgets: long while `live`, short while `stale`/`unknown`.
    live_budget: float = 1800.0
    stale_budget: float = 30.0
    unknown_budget: float = 60.0
    # §7.4 busy_reason hysteresis: keep a known reason this long after its on-screen marker vanishes.
    reason_ttl: float = 30.0
    # §6 hook precedence & lost-hook reconciliation (plan Task 7). Times are seconds.
    # startup grace after task-delivery for a hook-capable session: while hook_mode is "unknown"
    # within this window the screen stays conservative (no screen-derived free-text idle); grace
    # expired with no hook -> "unavailable" (screen-driven for the session's life, today's path).
    hook_startup_grace: float = 12.0
    # a stable free-text screen persisting this long after the last hook, while hooks say busy, is a
    # lost Stop -> reconcile to a low-confidence idle (never a respondable waiting_for_user).
    hook_turn_grace: float = 4.0
    # busy per hooks with no new hook AND no screen progress for this long -> lost-Stop timeout ->
    # intervention_required (a stuck agent, never silently idle).
    lost_stop_after: float = 45.0


@dataclass
class ExecutorSpec:
    command: str
    args: list
    env: dict
    driver: str
    launcher: str = "auto"
    # nelix-c5o: runtime-resolved env values. Each command's trimmed stdout becomes the value of the
    # named env var at spawn (see daemon/env_resolver.py); merged over static `env` and under the
    # NELIX_* hook env. `field(default_factory=dict)` because a bare `{}` default is a dataclass error.
    env_cmd: dict = field(default_factory=dict)
    env_cmd_timeout_seconds: float = 15.0    # per-command resolution deadline (seconds)
    settle_seconds: float = 1.5
    delivery_confirm_seconds: float = 10.0   # how long to wait for delivery confirmation before failing
    respond_write_seconds: float = 5.0       # deadline for the respond() PTY write (wedged-stdin guard)
    respond_confirm_seconds: float = 6.0     # window to confirm a respond's answer LEFT the box (nelix-sud)
    max_idle_seconds: float = 600.0      # pre-delivery blocked no-progress backstop; 0 = disabled
    # S6: observation grace — how long a session can go unobserved (no heartbeat,
    # no outstanding /wait) before being marked orphaned. Distinct from max_idle_seconds
    # (stalled-worker escalation) and startup_timeout_seconds (pre-delivery deadline).
    # Default 3600s (1 hour); 0 = disabled (no orphaning).
    observation_grace_seconds: float = 3600.0
    startup_timeout_seconds: float = 60.0  # pre-delivery startup deadline: no classifiable output within
                                         # this (from the readiness point) -> terminal fail; 0 = disabled
    max_restarts: int = 3                # recovery: consecutive auto-restarts before escalating (Hermes)
    # belief-engine tunables (spec §7; user-overridable). Defaults mirror config.BeliefConfig.
    post_submit_grace: float = 8.0       # §7.1 TTFT suppression bound
    echo_stuck_after: float = 20.0       # §7.1 never-clearing input box surfaces a wake (nelix-sud)
    idle_confirm_window: float = 0.5     # §7.1 suspicious-idle confirmation window
    live_budget: float = 1800.0          # §7.4 watchdog budget while liveness=live
    stale_budget: float = 30.0           # §7.4 watchdog budget while liveness=stale
    unknown_budget: float = 60.0         # §7.4 watchdog budget while liveness=unknown
    heartbeat_stale_after: float = 10.0  # §7.4 frozen-but-should-tick -> stale after this long
    tail_lines: int = 400
    status_tail_chars: int = 4000
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


def _spec_timeout(spec, key, default):
    """A per-executor timeout that must be a FINITE, strictly-positive number, else `default`.
    Unlike `_spec_num`, TOML `inf`/`nan` are rejected here (they make `subprocess.run(timeout=...)`
    never fire -> a hung /start, defeating the fail-closed contract) and 0/negatives are rejected
    (a non-positive timeout is meaningless). Bad value -> default (lenient), never skips the executor."""
    v = spec.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    v = float(v)
    if not math.isfinite(v) or v <= 0:
        return default
    return v


@dataclass
class ExecutorLoad:
    specs: dict                  # name -> ExecutorSpec (valid only)
    executor_errors: list        # [{"name": str, "problem": str}]
    parse_error: "str | None"    # whole-file TOML/IO error, else None


def _build_env_cmd(name, spec):
    """Parse + validate [executors.<name>.env_cmd]. Each KEY must be a non-empty string with no `=`
    (it becomes an env var name), and each VALUE a non-empty string (the command run at spawn). A bad
    entry raises ValueError -> per-executor load error (executor skipped, others still load)."""
    raw = spec.get("env_cmd", {})
    if not isinstance(raw, dict):
        raise ValueError(f"executor {name!r}: 'env_cmd' must be a table of VAR = command")
    out = {}
    for var, cmd in raw.items():
        if not isinstance(var, str) or var == "" or "=" in var:
            raise ValueError(
                f"executor {name!r}: env_cmd var {var!r} must be a non-empty name without '='")
        if not isinstance(cmd, str) or cmd == "":
            raise ValueError(
                f"executor {name!r}: env_cmd[{var!r}] must be a non-empty command string")
        out[var] = cmd
    return out


def _build_spec(name, spec):
    """Build one ExecutorSpec or raise (KeyError/TypeError/ValueError) with a clear,
    user-relayable message. The caller collects the raise as a per-executor error."""
    if not isinstance(spec, dict):
        raise ValueError(f"executor {name!r}: must be an [executors.{name}] table")
    if "command" not in spec:
        raise ValueError(f"executor {name!r}: 'command' is required")
    if "driver" not in spec:
        raise ValueError(f"executor {name!r}: 'driver' is required")
    return ExecutorSpec(
        command=spec["command"],
        args=list(spec.get("args", [])),
        env=dict(spec.get("env", {})),
        driver=spec["driver"],
        launcher=spec.get("launcher", "auto"),
        env_cmd=_build_env_cmd(name, spec),
        env_cmd_timeout_seconds=_spec_timeout(spec, "env_cmd_timeout_seconds", 15.0),
        settle_seconds=float(spec.get("settle_seconds", 1.5)),
        delivery_confirm_seconds=_spec_num(spec, "delivery_confirm_seconds", 10.0, cast=float),
        respond_write_seconds=_spec_num(spec, "respond_write_seconds", 5.0, cast=float),
        respond_confirm_seconds=_spec_num(spec, "respond_confirm_seconds", 6.0, cast=float),
        max_idle_seconds=_spec_num(spec, "max_idle_seconds", 600.0, cast=float),
        observation_grace_seconds=_spec_num(spec, "observation_grace_seconds", 3600.0, cast=float),
        startup_timeout_seconds=_spec_num(spec, "startup_timeout_seconds", 60.0, cast=float),
        max_restarts=_spec_num(spec, "max_restarts", 3, cast=int),
        post_submit_grace=_spec_num(spec, "post_submit_grace", 8.0, cast=float),
        echo_stuck_after=_spec_num(spec, "echo_stuck_after", 20.0, cast=float),
        idle_confirm_window=_spec_num(spec, "idle_confirm_window", 0.5, cast=float),
        live_budget=_spec_num(spec, "live_budget", 1800.0, cast=float),
        stale_budget=_spec_num(spec, "stale_budget", 30.0, cast=float),
        unknown_budget=_spec_num(spec, "unknown_budget", 60.0, cast=float),
        heartbeat_stale_after=_spec_num(spec, "heartbeat_stale_after", 10.0, cast=float),
        tail_lines=int(spec.get("tail_lines", 400)),
        status_tail_chars=int(spec.get("status_tail_chars", 4000)),
        spool_max_bytes=int(spec.get("spool_max_bytes", 8_388_608)),
    )


def load_executors(path):
    """Resilient per-executor load: skip malformed entries (collecting a structured error),
    keep the valid ones, and NEVER raise. A whole-file TOML/IO error yields zero specs plus
    a single parse_error. Single source of validation for both the daemon and the plugin."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return ExecutorLoad({}, [], f"config file not found: {path}")
    except (OSError, tomllib.TOMLDecodeError) as e:
        return ExecutorLoad({}, [], f"could not parse {path}: {e}")
    execs = data.get("executors", {})
    if not isinstance(execs, dict):
        return ExecutorLoad({}, [], "'executors' must be a table")
    specs, errors = {}, []
    for name, spec in execs.items():
        try:
            specs[name] = _build_spec(name, spec)
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            errors.append({"name": name, "problem": str(e)})
    return ExecutorLoad(specs, errors, None)


def load_concurrency_limit(path, default=5):
    """Top-level concurrency cap. Malformed TOML/IO or a non-int / bool / below-1 value
    falls back to `default` (mirrors load_retention's _cfg_int) — never crash the load.
    Default 5 is the supported concurrent-executor target; raise it in nelix.toml if needed."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return default
    v = data.get("concurrency_limit", default)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        return default
    return v


def load_idle_retained_limit(path, default=5):
    """Max number of retained `idle` sessions (a completed turn that stays alive awaiting a
    follow-up). An idle session does NOT occupy an active concurrency slot but is bounded here so
    completed-but-unclosed sessions cannot accumulate without bound. Defaults to the concurrency
    limit (pass it as `default`). Malformed TOML/IO or a non-int / bool / below-1 value falls back
    to `default` (mirrors load_concurrency_limit) — never crash the load."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return default
    v = data.get("idle_retained_limit", default)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        return default
    return v


def load_live_pty_limit(path, default=5):
    """Global live-PTY lease bound — a SEPARATE counter from the active concurrency bound.
    A session holds a live-PTY lease from start until terminal (an idle session still holds
    its PTY/process). Defaults to the active concurrency limit. Malformed TOML/IO or a non-int
    / bool / below-1 value falls back to `default` — never crash the load."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return default
    v = data.get("live_pty_limit", default)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        return default
    return v


def load_kill_grace_seconds(path, default=5.0):
    """Seconds between SIGTERM and SIGKILL when reaping a process group. Top-level (not
    per-executor): startup reconcile has only a child.json record, not an ExecutorSpec."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return default
    v = data.get("kill_grace_seconds", default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        return default
    return float(v)


@dataclass
class EventRingConfig:
    # max_history: bound on EVICTABLE delivery/history events (unresolved decisions are PINNED and
    # exempt — daemon/events.py). Default 2048: with the default concurrency (5 active + 5 idle
    # sessions) that is ~200 recent events per session, far more than a session accumulates between
    # a waiter's doorbell pulls, so normal operation never evicts a still-relevant delivery event —
    # yet the log can no longer grow without bound over a long-lived daemon.
    max_history: int = 2048
    # owner_floor: per-owner recent-history retention floor. A quiet owner keeps at least this many
    # of its most-recent evictable events even while another owner floods, so a cross-owner flood
    # cannot evict a quiet owner's recent delivery doorbell. Default 64 (~a turn's worth of
    # delivery/progress events); owners * floor stays well under max_history at the default limits.
    owner_floor: int = 64


def load_event_ring(path):
    """Event-ring bounds (daemon/events.py). Malformed TOML/IO or a non-int / bool / below-floor
    value falls back to the default (mirrors load_retention's _cfg_int) — never crash the load."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        data = {}
    return EventRingConfig(
        max_history=_cfg_int(data, "event_ring_max", 2048, floor=1),
        owner_floor=_cfg_int(data, "event_ring_owner_floor", 64, floor=0),
    )


@dataclass
class RetentionConfig:
    daemon_log_retain: int = 10
    session_retain: int = 20
    session_max_age_days: int = 7
    replay_horizon_seconds: int = 86400  # 24h — max retry/idempotency window for start dedup


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
        replay_horizon_seconds=_cfg_int(data, "replay_horizon_seconds", 86400, floor=0),
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
