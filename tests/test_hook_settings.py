import json
import os

from daemon.hook_settings import (
    claude_hook_settings_json,
    executor_message_instructions,
    hook_launch,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    text = executor_message_instructions()
    assert os.path.join(_REPO_ROOT, "bin", "nelix-question") in text
    assert os.path.join(_REPO_ROOT, "bin", "nelix-note") in text
    assert "--continuation-plan" in text                      # nelix-question requires it
    assert "AskUserQuestion" in text                          # contrasted with the blocking tool


def test_executor_message_instructions_static():
    # Session-agnostic like claude_hook_settings_json(): identical every call, no per-launch state.
    assert executor_message_instructions() == executor_message_instructions()
