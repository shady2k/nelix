"""Static `claude --settings` hook injection (spec §hooks injection). One responsibility: build the
additive hook config + the per-launch env, so a Claude agent reports its own lifecycle to the daemon.

`claude_hook_settings_json()` is a constant, session-agnostic `--settings` JSON: every hook fires the
SAME `curl` to the daemon's unix socket, addressing the session and authenticating purely through the
`$NELIX_SESSION` / `$NELIX_HOOK_SOCK` / `$NELIX_HOOK_SECRET` env placeholders (never a baked-in id) —
that is what keeps it identical every call and safe to reuse. `hook_launch(...)` pairs that static
JSON with the concrete env for one launch. The config is ADDITIVE (inline `--settings` only, per the
Task-1 gating spike) and never reads or writes the user's `~/.claude` config.
"""
import json

# Every hook posts its raw JSON body (`-d @-`) to POST /hook/<sid> on the daemon's RPC unix socket,
# authenticated by the per-session secret header. Kept short-timeout and best-effort (`|| true`) so a
# down/slow daemon never blocks or fails the agent's turn. Addressing is env-only => the JSON is static.
_HOOK_CMD = (
    'curl -s --max-time 5 --unix-socket "$NELIX_HOOK_SOCK" '
    '-H "X-Nelix-Hook-Secret: $NELIX_HOOK_SECRET" '
    '"http://x/hook/$NELIX_SESSION" -d @- || true'
)


def _entry(matcher: str) -> dict:
    return {"matcher": matcher, "hooks": [{"type": "command", "command": _HOOK_CMD}]}


# PreToolUse/PostToolUse register an explicit AskUserQuestion matcher (the modal boundary) PLUS a
# catch-all; every other event is a single catch-all. All 8 lifecycle events the normalizer maps.
_HOOKS = {
    "Stop": [_entry("")],
    "StopFailure": [_entry("")],
    "UserPromptSubmit": [_entry("")],
    "PreToolUse": [_entry("AskUserQuestion"), _entry("")],
    "PostToolUse": [_entry("AskUserQuestion"), _entry("")],
    "PostToolUseFailure": [_entry("")],
    "PermissionRequest": [_entry("")],
    "Notification": [_entry("")],
}

_SETTINGS_JSON = json.dumps({"hooks": _HOOKS})


def claude_hook_settings_json() -> str:
    """The static, session-agnostic `--settings` JSON. Identical every call."""
    return _SETTINGS_JSON


def hook_launch(session_id: str, sock_path: str, secret: str) -> dict:
    """Build the argv+env injection for one hook-capable launch: the static `--settings` JSON plus the
    three env vars its `curl` commands resolve at runtime."""
    return {
        "argv_extra": ["--settings", claude_hook_settings_json()],
        "env": {
            "NELIX_SESSION": session_id,
            "NELIX_HOOK_SOCK": sock_path,
            "NELIX_HOOK_SECRET": secret,
        },
    }
