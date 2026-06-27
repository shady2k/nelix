from pathlib import Path

import yaml  # PyYAML ships with the daemon? if not, parse minimally

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_uses_real_hermes_fields():
    data = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert data["name"] == "nelix"
    assert data["manifest_version"] == 1          # validated on install (plugins_cmd.py)
    assert data["kind"] == "standalone"
    # The parser reads `provides_hooks`, NOT `hooks` (plugins.py PluginManifest).
    # Teardown is on on_session_finalize (true teardown), not on_session_end (per-turn).
    assert "on_session_finalize" in data["provides_hooks"]
    assert set(data["provides_tools"]) == {"nelix_start", "nelix_status", "nelix_respond",
                                            "nelix_stop", "nelix_restart", "nelix_dialog"}
    # `pip_dependencies` is a no-op in Hermes — deps are installed venv-scoped by
    # supervisor._ensure_deps(), so the manifest must not pretend otherwise.
    assert "pip_dependencies" not in data


def test_example_is_debranded():
    text = (ROOT / "nelix.toml.example").read_text().lower()
    for leak in ("claude_zai", "/users/", "zai-wrapper", "envconsul"):
        assert leak not in text
