"""What the core still ships and must keep honest.

test_manifest_uses_real_hermes_fields lived here until the plugin was extracted; it asserted
plugin.yaml's Hermes fields, and went with plugin.yaml to shady2k/hermes-nelix
(tests/test_plugin_manifest.py) [nelix-4el.1]. What remains is nelix.toml.example, which is
core: daemon/config.py is what reads the nelix.toml it seeds.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_example_is_debranded():
    text = (ROOT / "nelix.toml.example").read_text().lower()
    for leak in ("claude_zai", "/users/", "zai-wrapper", "envconsul"):
        assert leak not in text
