import json
import os

import daemon.launchers.local as local
import paths
from daemon import hook_settings
from tests.conftest import make_spec


class FakeBroker:
    """Captures the exact argv/env the launcher hands the broker for spawn."""

    def __init__(self, captured):
        self.captured = captured

    def spawn(self, argv, cwd, env, cols, rows):
        self.captured["argv"] = argv
        self.captured["env"] = env
        self.captured["cwd"] = cwd
        return 7, 111, 111                       # master_fd, pid, pgid


class FakePty:
    def __init__(self, master_fd, pid, pgid, cols=120, rows=40, dialog=None, transcript=None):
        pass

    def close(self):
        pass


def _run(monkeypatch, spec, **kw):
    # Hermetic: our three injected vars must not leak from the ambient environment into the
    # "no injection" assertions (resolved_env() copies os.environ).
    for var in ("NELIX_SESSION", "NELIX_HOOK_SOCK", "NELIX_HOOK_SECRET"):
        monkeypatch.delenv(var, raising=False)
    # S1c-2: NELIX_RPC_SOCK is required for per-generation daemon hooks.
    monkeypatch.setenv("NELIX_RPC_SOCK", str(paths.router_sock()))
    captured = {}
    monkeypatch.setattr(local, "get_broker", lambda: FakeBroker(captured))
    monkeypatch.setattr(local, "PtySession", FakePty)
    local.LocalLauncher().start(spec, "/tmp", 80, 24, **kw)
    return captured


def test_local_launcher_injects_hooks_for_claude(monkeypatch):
    spec = make_spec(command="claude", args=["--foo"], driver="claude")
    cap = _run(monkeypatch, spec, session_id="s-1", hook_secret="sek")
    # original argv is preserved and --settings <json> is folded in
    assert cap["argv"][:2] == ["claude", "--foo"]
    assert "--settings" in cap["argv"]
    i = cap["argv"].index("--settings")
    json.loads(cap["argv"][i + 1])               # the value is valid JSON
    assert cap["env"]["NELIX_SESSION"] == "s-1"
    assert cap["env"]["NELIX_HOOK_SECRET"] == "sek"
    assert cap["env"]["NELIX_HOOK_SOCK"] == str(paths.router_sock())


def test_merges_user_supplied_settings_instead_of_clobbering(monkeypatch):
    # IMPORTANT 2: the user's nelix.toml executor args already carry a --settings (their own config).
    # nelix must NOT append a second --settings (Claude is last-wins, which would clobber the user's);
    # it merges our hooks ADDITIVELY into the user's settings value and passes ONE merged --settings.
    user_settings = json.dumps({
        "permissions": {"allow": ["Bash"]},
        "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "user-stop"}]}]},
    })
    spec = make_spec(command="claude", args=["--settings", user_settings], driver="claude")
    cap = _run(monkeypatch, spec, session_id="s-1", hook_secret="sek")
    argv = cap["argv"]
    assert argv.count("--settings") == 1                       # exactly ONE flag (no clobbering second)
    merged = json.loads(argv[argv.index("--settings") + 1])
    assert merged["permissions"] == {"allow": ["Bash"]}        # the user's non-hook keys preserved
    # our hooks are ADDITIVE: the user's Stop hook is KEPT and ours is appended alongside it.
    stop_cmds = [h["command"] for g in merged["hooks"]["Stop"] for h in g["hooks"]]
    assert "user-stop" in stop_cmds
    assert any("NELIX_HOOK_SECRET" in c for c in stop_cmds)    # our hook merged into the same event
    assert "UserPromptSubmit" in merged["hooks"]               # our other events are present too
    assert cap["env"]["NELIX_SESSION"] == "s-1"                # env still injected


def test_merges_user_settings_from_file_path(monkeypatch, tmp_path):
    # The user's --settings value may be a FILE PATH (Claude accepts inline JSON or a path). nelix
    # loads it, merges our hooks additively, and passes a single inline merged --settings.
    p = tmp_path / "user-settings.json"
    p.write_text(json.dumps({"model": "opus", "hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "user-pre"}]}]}}))
    spec = make_spec(command="claude", args=["--settings", str(p)], driver="claude")
    cap = _run(monkeypatch, spec, session_id="s-1", hook_secret="sek")
    argv = cap["argv"]
    assert argv.count("--settings") == 1
    merged = json.loads(argv[argv.index("--settings") + 1])
    assert merged["model"] == "opus"                           # user's non-hook keys preserved
    pre_cmds = [h["command"] for g in merged["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert "user-pre" in pre_cmds                              # user's PreToolUse hook kept
    assert any("NELIX_HOOK_SECRET" in c for c in pre_cmds)     # our PreToolUse hook merged alongside


def test_merges_user_settings_equals_form(monkeypatch):
    # The user's --settings may use the EQUALS form ("--settings=<value>"), not just the split form.
    # nelix must detect it too and merge; otherwise Claude's last-wins clobbers the user's settings.
    user_settings = json.dumps({
        "env": {"FOO": "bar"},
        "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "user-eq-stop"}]}]},
    })
    spec = make_spec(command="claude", args=["--settings=" + user_settings], driver="claude")
    cap = _run(monkeypatch, spec, session_id="s-1", hook_secret="sek")
    argv = cap["argv"]
    n = sum(1 for a in argv if a == "--settings" or a.startswith("--settings="))
    assert n == 1                                              # exactly ONE settings directive total
    if "--settings" in argv:
        merged = json.loads(argv[argv.index("--settings") + 1])
    else:
        eq = next(a for a in argv if a.startswith("--settings="))
        merged = json.loads(eq[len("--settings="):])
    assert merged["env"] == {"FOO": "bar"}                     # user's non-hook keys preserved
    stop_cmds = [h["command"] for g in merged["hooks"]["Stop"] for h in g["hooks"]]
    assert "user-eq-stop" in stop_cmds                         # user's hook kept
    assert any("NELIX_HOOK_SECRET" in c for c in stop_cmds)    # ours merged alongside


def test_collapses_multiple_user_settings(monkeypatch):
    # Multiple user --settings collapse to ONE (Claude last-wins among the user's own: the LAST value
    # is the effective base), with our hooks merged in; no stray extra --settings survives.
    s1 = json.dumps({"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "u1"}]}]}})
    s2 = json.dumps({"model": "opus", "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "u2"}]}]}})
    spec = make_spec(command="claude", args=["--settings", s1, "--settings=" + s2], driver="claude")
    cap = _run(monkeypatch, spec, session_id="s-1", hook_secret="sek")
    argv = cap["argv"]
    assert sum(1 for a in argv if a == "--settings" or a.startswith("--settings=")) == 1
    merged = json.loads(argv[argv.index("--settings") + 1])
    assert merged["model"] == "opus"                           # last user value is the base
    stop_cmds = [h["command"] for g in merged["hooks"]["Stop"] for h in g["hooks"]]
    assert "u2" in stop_cmds and any("NELIX_HOOK_SECRET" in c for c in stop_cmds)


def test_no_injection_for_non_hook_driver(monkeypatch):
    # codex is not a hook-capable (or even registered) driver -> nothing injected.
    spec = make_spec(command="codex", args=[], driver="codex")
    cap = _run(monkeypatch, spec, session_id="s-1", hook_secret="sek")
    assert "--settings" not in cap["argv"]
    assert "NELIX_SESSION" not in cap["env"]


def test_no_injection_without_session_and_secret(monkeypatch):
    # Hook-capable driver but the caller supplied neither id nor secret -> no injection.
    spec = make_spec(command="claude", args=[], driver="claude")
    cap = _run(monkeypatch, spec)
    assert "--settings" not in cap["argv"]
    assert "NELIX_SESSION" not in cap["env"]


def test_injects_executor_message_instructions_for_claude(monkeypatch):
    # The executor must be TOLD (via --append-system-prompt) that nelix-question/nelix-note exist,
    # by absolute path, without disturbing the existing --settings hook fold.
    #
    # Asserts the path the code RESOLVES, not a hardcoded <repo>/bin. This test hardcoded
    # <repo>/bin and was RED on main from 9b1a14e (nelix-9a4.1) until here: that slice rewrote
    # hook_settings._tool_path to prefer the INSTALLED console script (sysconfig scripts dir),
    # which requirements.txt's `-e .` creates — so the old assertion only held in a venv where
    # the core was NOT installed. Its pass/fail was a function of venv state rather than of the
    # code, which is why a green suite hid it (MEASURED: move the two console scripts out of
    # .venv/bin and it passes; put them back and it fails).
    #
    # The contract worth asserting is the one _tool_path was rewritten to enforce: the path nelix
    # hands a worker must actually EXIST and be runnable. Naming <site-packages>/bin/nelix-question
    # — a path that never exists — is the silent failure that rewrite was for. _tool_path's own
    # resolution order is covered hermetically in tests/test_hook_settings.py.
    spec = make_spec(command="claude", args=["--foo"], driver="claude")
    cap = _run(monkeypatch, spec, session_id="s-1", hook_secret="sek")
    argv = cap["argv"]
    assert argv.count("--append-system-prompt") == 1
    text = argv[argv.index("--append-system-prompt") + 1]
    for name in ("nelix-question", "nelix-note"):
        tool = hook_settings._tool_path(name)
        assert tool in text, f"instructions never name {name}"
        assert os.path.isabs(tool), f"{name} named by a relative path: {tool}"
        assert os.access(tool, os.X_OK), f"{name} named at a path that is not runnable: {tool}"
    assert "--settings" in argv                                # hook fold untouched
    json.loads(argv[argv.index("--settings") + 1])             # still valid JSON


def test_merges_user_append_system_prompt_instead_of_dropping_either(monkeypatch):
    # Claude's --append-system-prompt is LAST-WINS across repeated occurrences (verified empirically:
    # it does NOT concatenate) -- so nelix must fold its instruction into the user's existing flag
    # rather than appending a second occurrence, or one of the two texts would be silently dropped.
    spec = make_spec(command="claude", args=["--append-system-prompt", "USER_PROMPT_MARKER"],
                      driver="claude")
    cap = _run(monkeypatch, spec, session_id="s-1", hook_secret="sek")
    argv = cap["argv"]
    assert argv.count("--append-system-prompt") == 1
    text = argv[argv.index("--append-system-prompt") + 1]
    assert "USER_PROMPT_MARKER" in text                        # user's own text preserved
    assert "nelix-question" in text                            # ours merged in alongside it


def test_no_system_prompt_injection_for_non_hook_driver(monkeypatch):
    spec = make_spec(command="codex", args=[], driver="codex")
    cap = _run(monkeypatch, spec, session_id="s-1", hook_secret="sek")
    assert "--append-system-prompt" not in cap["argv"]


def test_no_system_prompt_injection_without_session_and_secret(monkeypatch):
    spec = make_spec(command="claude", args=[], driver="claude")
    cap = _run(monkeypatch, spec)
    assert "--append-system-prompt" not in cap["argv"]
