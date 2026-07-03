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
import os

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


def executor_message_instructions() -> str:
    """Concise, static text (identical every call, like `claude_hook_settings_json()`) telling a
    hook-capable executor that `nelix-question`/`nelix-note` exist as non-blocking alternatives to
    `AskUserQuestion`. Folded into the launch via `--append-system-prompt` in
    daemon/launchers/local.py (`_fold_system_prompt`), gated on the SAME hook-capable check as the
    hook `--settings` injection above -- a non-claude/hookless executor never sees it.

    Bin paths are computed ABSOLUTE from the repo root (this file lives at <root>/daemon/
    hook_settings.py, so one `dirname` off its own dir reaches <root>) and referenced by full path in
    the instruction text so the executor's PATH is never touched."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    question = os.path.join(root, "bin", "nelix-question")
    note = os.path.join(root, "bin", "nelix-note")
    return (
        "Nelix orchestration commands are available (invoke by the absolute path shown; nelix "
        "never modifies your PATH):\n"
        f'- Ask the orchestrator a question WITHOUT stopping work: `{question} --question "..." '
        '--continuation-plan "<what you will do while waiting>" [--assumption "..."] '
        '[--impact-if-wrong "..."]`. It returns immediately -- keep working under your stated '
        "assumption; the answer arrives later as a new message.\n"
        f'- Report progress without interrupting: `{note} --summary "..." [--details "..."]`.\n'
        "- Use AskUserQuestion only when you genuinely cannot proceed without an answer (it blocks "
        "your entire turn); prefer nelix-question whenever you can continue under a stated "
        "assumption."
    )


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
