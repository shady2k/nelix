import json
import os
import re
from pathlib import Path

import pytest

from daemon import hook_settings
from daemon.hook_settings import (
    _tool_path,
    claude_hook_settings_json,
    executor_message_instructions,
    hook_launch,
)

ROOT = Path(__file__).resolve().parents[1]


def test_settings_static_and_valid_json():
    a, b = claude_hook_settings_json(), claude_hook_settings_json()
    assert a == b                                   # identical every call
    cfg = json.loads(a)
    assert set(cfg["hooks"]) >= {"Stop", "StopFailure", "UserPromptSubmit", "PreToolUse",
                                 "PostToolUse", "PostToolUseFailure", "PermissionRequest",
                                 "Notification"}


def test_settings_command_uses_env_placeholders_only():
    s = claude_hook_settings_json()
    assert "$NELIX_HOOK_SOCK" in s and "$NELIX_SESSION" in s and "$NELIX_HOOK_SECRET" in s
    assert "s-" not in s                             # no baked session id -> static


def test_hook_launch_argv_and_env():
    out = hook_launch("s-abc", "/x/rpc.sock", "secretxyz")
    assert out["argv_extra"][0] == "--settings"
    json.loads(out["argv_extra"][1])                 # valid
    assert out["env"] == {"NELIX_SESSION": "s-abc", "NELIX_HOOK_SOCK": "/x/rpc.sock",
                          "NELIX_HOOK_SECRET": "secretxyz"}


def test_executor_message_instructions_mentions_commands_by_absolute_path():
    # The executor is told about nelix-question/nelix-note by ABSOLUTE path (never via PATH, which
    # nelix never mutates) so it can invoke them regardless of its own cwd or shell configuration.
    #
    # Asserted as a PROPERTY (absolute + exists + executable), not as a literal repo path: the path
    # legitimately differs between a checkout (bin/) and an install (<venv>/bin/), and this file
    # runs from a checkout whose venv may or may not have `-e .`. Pinning the repo layout here is
    # what let the installed daemon emit a nonexistent path unnoticed. tests/test_real_wheel.py
    # asserts the same property against a real install, which is the case this file cannot reach.
    text = executor_message_instructions()
    paths = re.findall(r"`(/[^\s`]+/nelix-(?:question|note))\b", text)
    assert {os.path.basename(p) for p in paths} == {"nelix-question", "nelix-note"}
    for p in paths:
        assert os.path.isabs(p)
        assert os.path.exists(p), f"instructions name {p}, which does not exist"
        assert os.access(p, os.X_OK), f"{p} is not executable"
    assert "--continuation-plan" in text                      # nelix-question requires it
    assert "AskUserQuestion" in text                          # contrasted with the blocking tool


def _fake_scripts_dir(monkeypatch, path):
    monkeypatch.setattr(hook_settings.sysconfig, "get_path",
                        lambda name, *a, **k: str(path) if name == "scripts" else "")


def test_tool_path_prefers_the_installed_console_script(tmp_path, monkeypatch):
    # Installed, the console script sits in this interpreter's script dir; that is the copy matching
    # the interpreter importing this module, so it wins over any checkout bin/.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "nelix-question"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    _fake_scripts_dir(monkeypatch, bindir)
    assert _tool_path("nelix-question") == str(script)


def test_tool_path_fails_closed_when_the_tool_is_missing(tmp_path, monkeypatch):
    # The silent failure this replaces: returning <site-packages>/bin/nelix-question, a path that
    # does not exist, so the worker just never uses the tool and nobody learns why.
    _fake_scripts_dir(monkeypatch, tmp_path / "bin")
    monkeypatch.setattr(hook_settings, "__file__", str(tmp_path / "site-packages" / "daemon" / "x.py"))
    with pytest.raises(RuntimeError, match="cannot locate the executor CLI"):
        _tool_path("nelix-question")


def test_tool_path_ignores_a_non_executable_file(tmp_path, monkeypatch):
    # A +x check, not just existence: a console script that lost its bit is not runnable, and
    # naming it to a worker is the same silent dead end as naming a path that is absent.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "nelix-note").write_text("#!/bin/sh\n")     # no chmod
    _fake_scripts_dir(monkeypatch, bindir)
    assert _tool_path("nelix-note") == str(ROOT / "bin" / "nelix-note")    # falls through


def test_executor_message_instructions_static():
    # Session-agnostic like claude_hook_settings_json(): identical every call, no per-launch state.
    assert executor_message_instructions() == executor_message_instructions()
