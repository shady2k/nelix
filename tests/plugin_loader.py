import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]  # repo root = the plugin dir


def load_plugin(module_name="hermes_plugins.nelix"):
    """Import the plugin package exactly as Hermes' loader does."""
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    init_file = PLUGIN_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        module_name, init_file, submodule_search_locations=[str(PLUGIN_ROOT)])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = module_name
    mod.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod
