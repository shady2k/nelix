import json

from daemon.hook_settings import claude_hook_settings_json, hook_launch


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
