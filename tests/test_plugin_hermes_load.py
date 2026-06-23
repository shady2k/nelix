import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "plugin"

# Mimics hermes_cli.plugins._load_directory_module: import the plugin's
# __init__.py as ``hermes_plugins.nelix`` with submodule_search_locations, in a
# fresh interpreter whose sys.path does NOT expose ``plugin``/``daemon`` as
# top-level packages (Hermes' launcher runs with PYTHONPATH unset). This is the
# faithful production load path; importing ``plugin`` directly (as the other
# tests do) only works because pytest runs from the repo root.
_LOADER = textwrap.dedent("""
    import importlib.util, types, sys
    plugin_dir = sys.argv[1]
    ns = types.ModuleType("hermes_plugins"); ns.__path__ = []
    sys.modules["hermes_plugins"] = ns
    name = "hermes_plugins.nelix"
    spec = importlib.util.spec_from_file_location(
        name, plugin_dir + "/__init__.py", submodule_search_locations=[plugin_dir])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = name
    mod.__path__ = [plugin_dir]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    assert callable(getattr(mod, "register", None)), "no register()"
    print("OK")
""")


def test_plugin_loads_the_way_hermes_loads_it(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", _LOADER, str(PLUGIN_DIR)],
        cwd=str(tmp_path), capture_output=True, text=True)
    assert proc.returncode == 0, f"plugin failed to load like Hermes:\n{proc.stderr}"
    assert "OK" in proc.stdout
