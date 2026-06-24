import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_LOADER = textwrap.dedent("""
    import importlib.util, types, sys
    root = sys.argv[1]
    ns = types.ModuleType("hermes_plugins"); ns.__path__ = []
    sys.modules["hermes_plugins"] = ns
    name = "hermes_plugins.nelix"
    spec = importlib.util.spec_from_file_location(
        name, root + "/__init__.py", submodule_search_locations=[root])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = name
    mod.__path__ = [root]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    assert callable(getattr(mod, "register", None)), "no register()"
    print("OK")
""")


def test_plugin_loads_the_way_hermes_loads_it(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", _LOADER, str(REPO)],
        cwd=str(tmp_path), capture_output=True, text=True)
    assert proc.returncode == 0, f"plugin failed to load like Hermes:\n{proc.stderr}"
    assert "OK" in proc.stdout
