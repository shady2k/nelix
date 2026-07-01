import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_doctor():
    import importlib.util
    import importlib.machinery
    p = Path(__file__).resolve().parents[1] / "bin" / "nelix-doctor"
    loader = importlib.machinery.SourceFileLoader("nelix_doctor", str(p))
    spec = importlib.util.spec_from_loader("nelix_doctor", loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_doctor_collects_strays(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import paths
    importlib.reload(paths)
    from daemon import reaper
    sd = paths.sessions_root() / "s-stray1"; sd.mkdir(parents=True)
    reaper.record_child(sd, {"sid": "s-stray1", "daemon_pid": 10, "daemon_fingerprint": "d",
                             "pid": 999, "child_fingerprint": "c", "pgid": 999, "argv": ["x"]})
    doctor = _load_doctor()

    class _Insp:
        def is_alive(self, pid): return pid == 999          # daemon 10 dead, child 999 alive
        def start_fingerprint(self, pid): return "c" if pid == 999 else "?"
        def pgid(self, pid): return 999
        def ppid(self, pid): return 1
    monkeypatch.setattr(doctor.supervisor, "endpoint", lambda: None)   # no live daemon
    out = doctor.collect(inspector=_Insp())
    assert any(s["sid"] == "s-stray1" for s in out["strays"])
    assert out["daemon"]["alive"] is False


# --- hermes_wiring: does the profile the user launched actually load nelix? -------------

import yaml  # noqa: E402  (PyYAML ships with the tooling venv; used across the suite)


def _write_config(path, enabled):
    """Write a Hermes config.yaml with the given plugins.enabled list."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"plugins": {"enabled": list(enabled)}}))


def _profile(wiring, name):
    for p in wiring["profiles"]:
        if p["name"] == name:
            return p
    raise AssertionError(f"profile {name!r} not in {[p['name'] for p in wiring['profiles']]}")


def test_hermes_wiring_enabled_and_installed_is_wired(tmp_path):
    """(a) A profile that enables nelix AND has the plugin installed → verdict 'wired'."""
    doctor = _load_doctor()
    _write_config(tmp_path / "config.yaml", ["nelix"])
    (tmp_path / "plugins" / "nelix").mkdir(parents=True)
    (tmp_path / "workspace" / "nelix").mkdir(parents=True)
    (tmp_path / "workspace" / "nelix" / "nelix.toml").write_text("")
    root = _profile(doctor.hermes_wiring(home=tmp_path), "(root)")
    assert root["nelix_enabled"] is True
    assert root["plugin_installed"] is True
    assert root["nelix_toml_present"] is True
    assert root["verdict"] == "wired"
    assert root["reason"] is None


def test_hermes_wiring_empty_enabled_is_not_wired_naming_the_cause(tmp_path):
    """(b) plugins.enabled: [] → 'not_wired', reason names the missing enable entry."""
    doctor = _load_doctor()
    _write_config(tmp_path / "config.yaml", [])
    (tmp_path / "plugins" / "nelix").mkdir(parents=True)   # installed, but not enabled
    root = _profile(doctor.hermes_wiring(home=tmp_path), "(root)")
    assert root["nelix_enabled"] is False
    assert root["verdict"] == "not_wired"
    assert "plugins.enabled" in root["reason"]


def test_hermes_wiring_missing_plugin_dir_is_not_wired(tmp_path):
    """(c) Enabled but the plugin dir is absent → 'not_wired', reason names the missing dir."""
    doctor = _load_doctor()
    _write_config(tmp_path / "config.yaml", ["nelix"])   # enabled, but never installed
    root = _profile(doctor.hermes_wiring(home=tmp_path), "(root)")
    assert root["nelix_enabled"] is True
    assert root["plugin_installed"] is False
    assert root["verdict"] == "not_wired"
    assert "dir" in root["reason"]


def test_hermes_wiring_lists_named_profiles_and_tolerates_null_plugins(tmp_path):
    """Root plus every named profile is reported; a `plugins: null` config does not crash."""
    doctor = _load_doctor()
    _write_config(tmp_path / "config.yaml", [])
    loc = tmp_path / "profiles" / "local"
    _write_config(loc / "config.yaml", ["nelix"])
    (loc / "plugins" / "nelix").mkdir(parents=True)
    work = tmp_path / "profiles" / "work"
    work.mkdir(parents=True)
    (work / "config.yaml").write_text("plugins:\n")       # plugins: null
    wiring = doctor.hermes_wiring(home=tmp_path)
    assert {"(root)", "local", "work"} <= {p["name"] for p in wiring["profiles"]}
    assert _profile(wiring, "local")["verdict"] == "wired"
    assert _profile(wiring, "work")["nelix_enabled"] is False
    assert _profile(wiring, "work")["verdict"] == "not_wired"


def test_hermes_wiring_defaults_to_paths_hermes_home(monkeypatch, tmp_path):
    """With no home arg it resolves via paths.hermes_home() (reuses the shared helper)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import paths
    importlib.reload(paths)
    _write_config(tmp_path / "config.yaml", ["nelix"])
    (tmp_path / "plugins" / "nelix").mkdir(parents=True)
    doctor = _load_doctor()
    wiring = doctor.hermes_wiring()
    assert wiring["home"] == str(tmp_path)
    assert _profile(wiring, "(root)")["verdict"] == "wired"


# --- manifest_drift: plugin.yaml provides_tools vs ctx.register_tool(...) names ---------


def test_manifest_drift_flags_registered_but_undeclared_tool(tmp_path):
    """(d) A tool registered in __init__.py but absent from provides_tools is reported."""
    doctor = _load_doctor()
    pj = tmp_path / "plugin.yaml"
    pj.write_text("provides_tools: [nelix_start, nelix_status]\n")
    init = tmp_path / "__init__.py"
    init.write_text(
        "def register(ctx):\n"
        "    ctx.register_tool('nelix_start', 'nelix', {}, None)\n"
        "    ctx.register_tool('nelix_status', 'nelix', {}, None)\n"
        "    ctx.register_tool('nelix_screen', 'nelix', {}, None)\n"
    )
    drift = doctor.manifest_drift(plugin_yaml=pj, init_path=init)
    assert "nelix_screen" in drift["registered"]
    assert "nelix_screen" not in drift["declared"]
    assert drift["missing_from_manifest"] == ["nelix_screen"]
    assert drift["extra_in_manifest"] == []


def test_collect_adds_new_sections_and_keeps_old_keys(monkeypatch, tmp_path):
    """collect() gains hermes_wiring + manifest_drift without dropping existing consumers' keys."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import paths
    importlib.reload(paths)
    doctor = _load_doctor()
    monkeypatch.setattr(doctor.supervisor, "endpoint", lambda: None)
    out = doctor.collect()
    for key in ("daemon", "lock_holder", "sessions", "strays"):
        assert key in out
    assert "profiles" in out["hermes_wiring"]
    assert "registered" in out["manifest_drift"]


def test_real_manifest_declares_every_registered_tool():
    """Truthfulness: plugin.yaml provides_tools must match the tools __init__.py registers,
    with no drift in either direction — including nelix_screen."""
    doctor = _load_doctor()
    drift = doctor.manifest_drift()
    assert drift["missing_from_manifest"] == []
    assert drift["extra_in_manifest"] == []
    assert "nelix_screen" in drift["declared"]
