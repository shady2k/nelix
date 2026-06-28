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


# Regression: supervisor._healthy() did a bare `from rpc_client import RpcClient`
# with no relative fallback (unlike the dual-mode block at the top of supervisor.py),
# so the FIRST nelix_start in package mode died with
#   ModuleNotFoundError: No module named 'rpc_client'
# before any daemon was contacted. With no daemon up, _healthy() must return False.
_HEALTH_LOADER = textwrap.dedent("""
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
    sup = sys.modules["hermes_plugins.nelix.supervisor"]
    t = sup.Transport.unix(root + "/does-not-exist-nelix.sock")
    assert sup._healthy(t) is False, "expected _healthy() -> False, not an import crash"
    print("OK")
""")


def test_healthy_check_works_in_package_mode(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", _HEALTH_LOADER, str(REPO)],
        cwd=str(tmp_path), capture_output=True, text=True)
    assert proc.returncode == 0, f"_healthy() broke under package-mode load:\n{proc.stderr}"
    assert "OK" in proc.stdout


# reaper/singleton bare-import `paths`; the supervisor now imports them in-process (package mode)
# to reconcile the daemon lock holder. Importing them as package submodules must NOT raise the
# `No module named 'paths'` that bit _healthy() — guard the dual-mode `paths` import.
_DAEMON_PKG_LOADER = textwrap.dedent("""
    import importlib, importlib.util, types, sys
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
    r = importlib.import_module("hermes_plugins.nelix.daemon.reaper")
    s = importlib.import_module("hermes_plugins.nelix.daemon.singleton")
    assert hasattr(r, "ProcessInspector") and hasattr(s, "acquire"), "missing symbols"
    print("OK")
""")


def test_reaper_and_singleton_import_in_package_mode(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", _DAEMON_PKG_LOADER, str(REPO)],
        cwd=str(tmp_path), capture_output=True, text=True)
    assert proc.returncode == 0, f"reaper/singleton not package-safe:\n{proc.stderr}"
    assert "OK" in proc.stdout
