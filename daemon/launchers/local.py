import json
import os

import paths
from daemon.broker_client import get_broker
from daemon.drivers import DRIVERS
from daemon.hook_settings import executor_message_instructions, hook_launch
from daemon.launchers.base import ExecutorCapabilities
from daemon.pty_session import PtySession


def _driver_hook_capable(driver_name):
    # Read the class attribute off the registry (never instantiates). An unregistered/unknown
    # driver is treated as NOT hook-capable, so the launcher never crashes on a stray name and
    # only injects for a driver that opted in (ClaudeDriver.hook_capable = True).
    cls = DRIVERS.get(driver_name)
    return bool(getattr(cls, "hook_capable", False))


def _load_user_settings(value):
    # The user's --settings value (from nelix.toml executor args) is either inline JSON or a path to
    # a settings file — Claude accepts both. Try JSON first, then a file. On any failure return {} so
    # we merge our hooks onto an empty base rather than clobber or crash the launch.
    try:
        obj = json.loads(value)
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass
    try:
        with open(os.path.expanduser(value)) as f:
            obj = json.load(f)
        if isinstance(obj, dict):
            return obj
    except (OSError, ValueError):
        pass
    return {}


def _deep_merge_hooks(base, ours):
    # ADDITIVE merge of our hooks into the user's settings (spec §3: our hooks run ALONGSIDE the
    # user's, never replacing them). Every user key is preserved untouched; only "hooks" is extended:
    # for each hook event, our matcher-groups are APPENDED to the user's existing list for that event.
    merged = dict(base)
    hooks = {k: list(v) if isinstance(v, list) else v for k, v in (base.get("hooks") or {}).items()}
    for event, entries in (ours.get("hooks") or {}).items():
        hooks[event] = [*(hooks.get(event) or []), *entries]
    merged["hooks"] = hooks
    return merged


def _split_settings_args(argv):
    # Strip EVERY user --settings from argv (both "--settings <value>" and "--settings=<value>" forms)
    # and return (cleaned_argv, effective_user_value). Claude is last-wins among the user's own flags,
    # so the effective value is the LAST one seen (None if the user supplied none / only a valueless flag).
    cleaned, value, i, n = [], None, 0, len(argv)
    while i < n:
        a = argv[i]
        if a == "--settings":
            if i + 1 < n:
                value = argv[i + 1]
                i += 2
                continue
            i += 1                                  # malformed trailing flag (no value): drop it
            continue
        if a.startswith("--settings="):
            value = a[len("--settings="):]
            i += 1
            continue
        cleaned.append(a)
        i += 1
    return cleaned, value


def _fold_hook_settings(argv, argv_extra):
    # Fold our ["--settings", <hooks json>] into argv WITHOUT clobbering a user-supplied --settings.
    # Claude is last-wins, so a second flag would drop the user's. Detect their --settings in BOTH
    # forms, take their effective (last) value as the base, MERGE our hooks additively into it, and
    # emit a SINGLE normalized "--settings <merged>". If the user supplied none, append ours as-is.
    our_json = argv_extra[argv_extra.index("--settings") + 1]
    cleaned, user_value = _split_settings_args(argv)
    if user_value is None:
        return [*cleaned, *argv_extra]
    merged = _deep_merge_hooks(_load_user_settings(user_value), json.loads(our_json))
    return [*cleaned, "--settings", json.dumps(merged)]


def _read_text_file(path):
    # Best-effort read of a user-supplied --append-system-prompt-file target. On any failure return
    # None so the caller falls back to "no user text" rather than crashing the launch.
    try:
        with open(os.path.expanduser(path)) as f:
            return f.read()
    except OSError:
        return None


def _split_system_prompt_args(argv):
    # Strip EVERY user --append-system-prompt (both "--append-system-prompt <v>" and
    # "--append-system-prompt=<v>") AND --append-system-prompt-file (both forms) from argv, returning
    # (cleaned_argv, effective_text). Verified empirically against the real `claude` CLI: repeated
    # --append-system-prompt flags do NOT concatenate -- the LAST occurrence wins, and mixing the
    # inline and -file forms is a hard CLI error ("Cannot use both --append-system-prompt and
    # --append-system-prompt-file"). So the effective user text is whichever form's flag appears
    # LAST in argv, matching Claude's own last-wins semantics; a -file value is read from disk.
    cleaned, text, i, n = [], None, 0, len(argv)
    while i < n:
        a = argv[i]
        if a == "--append-system-prompt":
            if i + 1 < n:
                text = argv[i + 1]
                i += 2
                continue
            i += 1                                  # malformed trailing flag (no value): drop it
            continue
        if a.startswith("--append-system-prompt="):
            text = a[len("--append-system-prompt="):]
            i += 1
            continue
        if a == "--append-system-prompt-file":
            if i + 1 < n:
                text = _read_text_file(argv[i + 1])
                i += 2
                continue
            i += 1
            continue
        if a.startswith("--append-system-prompt-file="):
            text = _read_text_file(a[len("--append-system-prompt-file="):])
            i += 1
            continue
        cleaned.append(a)
        i += 1
    return cleaned, text


def _fold_system_prompt(argv, instruction):
    # Fold our executor instruction into a SINGLE --append-system-prompt. Claude does NOT concatenate
    # repeated occurrences of this flag (see _split_system_prompt_args) -- appending a second one
    # would silently drop either the user's system-prompt text or ours, defeating either the user's
    # config or the whole point of this injection. Instead: pull out the user's effective text (if
    # any), concatenate ours after it, and emit exactly one inline flag.
    cleaned, user_text = _split_system_prompt_args(argv)
    merged = f"{user_text}\n\n{instruction}" if user_text else instruction
    return [*cleaned, "--append-system-prompt", merged]


class LocalLauncher:
    """Run the executor as a host process in a PTY. Isolation == host. The actual fork+exec
    happens in the single-threaded broker (daemon.pty_broker), never in the daemon."""

    capabilities = ExecutorCapabilities(isolation_class="host", can_attach=False)

    def start(self, spec, cwd, cols=120, rows=40, dialog=None, transcript=None,
              *, session_id=None, hook_secret=None):
        argv = spec.argv()
        env = spec.resolved_env()
        # For a hook-capable driver with a session id + per-session secret, fold the additive
        # --settings hook config into argv and the NELIX_* addressing into env BEFORE the spawn.
        # Never touches the user's config; injection is skipped for hookless drivers (fallback path).
        if session_id and hook_secret and _driver_hook_capable(spec.driver):
            inj = hook_launch(session_id, str(paths.rpc_sock()), hook_secret)
            argv = _fold_hook_settings(argv, inj["argv_extra"])
            argv = _fold_system_prompt(argv, executor_message_instructions())
            env = {**env, **inj["env"]}
        master_fd, pid, pgid = get_broker().spawn(argv, cwd, env, cols, rows)
        return PtySession(master_fd, pid, pgid, cols=cols, rows=rows,
                          dialog=dialog, transcript=transcript)

    def stop(self, handle):
        handle.close()
