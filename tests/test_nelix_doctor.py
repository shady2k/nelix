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
    monkeypatch.setattr(doctor.supervisor, "base_token", lambda: None)   # no live daemon
    out = doctor.collect(inspector=_Insp())
    assert any(s["sid"] == "s-stray1" for s in out["strays"])
    assert out["daemon"]["alive"] is False
