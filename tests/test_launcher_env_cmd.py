"""nelix-c5o: LocalLauncher merges resolved env_cmd into the spawn env.

Asserts on the exact env the launcher hands the broker (FakeBroker captures it) — the real
resolver runs real commands, no mocking, no fabricated PTY frames. Ordering matters:
env_cmd overrides static [env] but must NOT override the injected NELIX_* hook env.
"""
import os

import pytest

import daemon.launchers.local as local
from conftest import make_spec
from daemon.env_resolver import EnvResolveError


class FakeBroker:
    def __init__(self, captured):
        self.captured = captured

    def spawn(self, argv, cwd, env, cols, rows):
        self.captured["argv"] = argv
        self.captured["env"] = env
        return 7, 111, 111


class FakePty:
    def __init__(self, *a, **k):
        pass

    def close(self):
        pass


def _run(monkeypatch, spec, **kw):
    for var in ("NELIX_SESSION", "NELIX_HOOK_SOCK", "NELIX_HOOK_SECRET"):
        monkeypatch.delenv(var, raising=False)
    captured = {}
    monkeypatch.setattr(local, "get_broker", lambda: FakeBroker(captured))
    monkeypatch.setattr(local, "PtySession", FakePty)
    local.LocalLauncher().start(spec, "/tmp", 80, 24, **kw)
    return captured


def test_env_cmd_result_reaches_spawn_env(monkeypatch):
    spec = make_spec(command="claude", driver="claude", env_cmd={"TOK": "echo secret-val"})
    cap = _run(monkeypatch, spec)
    assert cap["env"]["TOK"] == "secret-val"          # command stdout is the child's env value


def test_env_cmd_overrides_static_env(monkeypatch):
    spec = make_spec(command="claude", driver="claude",
                     env={"TOK": "static"}, env_cmd={"TOK": "echo dynamic"})
    cap = _run(monkeypatch, spec)
    assert cap["env"]["TOK"] == "dynamic"             # env_cmd wins over static [env]


def test_env_cmd_sees_daemon_ambient_env(monkeypatch):
    # local.py passes os.environ as base_env, so the command can read whatever the daemon has.
    monkeypatch.setenv("NELIX_C5O_SEED", "from-ambient")
    spec = make_spec(command="claude", driver="claude", env_cmd={"V": 'printf %s "$NELIX_C5O_SEED"'})
    cap = _run(monkeypatch, spec)
    assert cap["env"]["V"] == "from-ambient"


def test_env_cmd_cannot_override_injected_nelix_hook_env(monkeypatch):
    # Hook injection stays LAST: even an env_cmd targeting a NELIX_* var cannot displace the real
    # hook addressing (that would break hook auth/reporting).
    spec = make_spec(command="claude", driver="claude",
                     env_cmd={"NELIX_SESSION": "echo HACKED", "TOK": "echo ok"})
    cap = _run(monkeypatch, spec, session_id="s-1", hook_secret="sek")
    assert cap["env"]["NELIX_SESSION"] == "s-1"       # hook env wins
    assert cap["env"]["TOK"] == "ok"                  # a non-NELIX env_cmd still applies


def test_no_env_cmd_spawn_env_is_byte_identical(monkeypatch):
    # No env_cmd (the default) -> spawn env is exactly resolved_env(), byte-identical to pre-feature.
    spec = make_spec(command="codex", driver="codex")     # non-hook driver: no NELIX_* either
    cap = _run(monkeypatch, spec)
    assert cap["env"] == spec.resolved_env()


def test_env_cmd_failure_raises_before_spawn(monkeypatch):
    # A failing resolver aborts the launch BEFORE the broker is ever asked to spawn.
    captured = {}
    monkeypatch.setattr(local, "get_broker", lambda: FakeBroker(captured))
    monkeypatch.setattr(local, "PtySession", FakePty)
    spec = make_spec(command="claude", driver="claude", env_cmd={"TOK": "exit 4"})
    with pytest.raises(EnvResolveError) as ei:
        local.LocalLauncher().start(spec, "/tmp", 80, 24)
    assert ei.value.var == "TOK"
    assert "env" not in captured                       # broker.spawn never reached
