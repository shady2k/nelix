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
import sysconfig
from pathlib import Path

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


def _tool_path(name: str) -> str:
    """Absolute path to an executor-facing CLI (`nelix-question`/`nelix-note`), resolved for the
    interpreter that is ACTUALLY running -- installed or from a checkout.

    Not `dirname(dirname(__file__))/bin`: that reaches the repo root only in a source tree. Once the
    core is installed, this file is `<venv>/lib/python3.11/site-packages/daemon/hook_settings.py`,
    so that derivation names `<site-packages>/bin/nelix-question`, which never exists -- and the old
    code returned it anyway. The daemon then handed a worker an instruction naming a path that isn't
    there, the worker silently never used the tool, and nobody learned why. Hence: probe, and raise.

    1. `sysconfig.get_path("scripts")/<name>` -- where an installer puts a console script for THIS
       interpreter. Correct for the install, and correct for a checkout whose venv has `-e .`
       (requirements.txt), because that installs the same console scripts.
    2. `<repo root>/bin/<name>` -- a checkout whose venv does NOT have the core installed. The suite
       runs this way, and so does anyone who cloned and ran the daemon straight out of the tree.

    `sysconfig` and not `Path(sys.executable).resolve().parent`, which is the obvious guess and is
    wrong here: a uv-created venv symlinks `bin/python` at the uv-managed base CPython, so resolving
    it walks OUT of the venv to `~/.local/share/uv/python/cpython-3.11.15-*/bin` and misses the
    scripts entirely. That is not exotic -- it is what this repo's own `.venv` does. `sysconfig`
    derives the script dir from `sys.prefix`, which a venv sets and a symlink cannot confuse.

    PATH is deliberately not consulted at either step: nelix never mutates the executor's PATH, and
    a `nelix-question` picked up from some unrelated PATH entry is not this core's.
    """
    candidates = (Path(sysconfig.get_path("scripts")) / name,
                  Path(__file__).resolve().parents[1] / "bin" / name)
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    raise RuntimeError(
        f"nelix: cannot locate the executor CLI {name!r}; looked at "
        + ", ".join(str(c) for c in candidates)
        + ". The core is installed without its console scripts, or this checkout's bin/ is gone. "
          "Refusing to tell a worker to run a path that does not exist.")


def executor_message_instructions() -> str:
    """Concise, static text (identical every call, like `claude_hook_settings_json()`) telling a
    hook-capable executor that `nelix-question`/`nelix-note` exist as non-blocking alternatives to
    `AskUserQuestion`. Folded into the launch via `--append-system-prompt` in
    daemon/launchers/local.py (`_fold_system_prompt`), gated on the SAME hook-capable check as the
    hook `--settings` injection above -- a non-claude/hookless executor never sees it.

    Tool paths are ABSOLUTE and referenced by full path in the instruction text, so the executor's
    PATH is never touched. See `_tool_path` for where they come from -- NOT from the repo layout,
    which is only one of the two places this code runs."""
    question = _tool_path("nelix-question")
    note = _tool_path("nelix-note")
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
