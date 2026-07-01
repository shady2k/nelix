import json

import daemon.launchers.local as local
import paths
from conftest import make_spec


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
    assert cap["env"]["NELIX_HOOK_SOCK"] == str(paths.rpc_sock())


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
