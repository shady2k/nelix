import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.transport import Transport  # noqa: E402


def _load_reap():
    import importlib.util
    from importlib.machinery import SourceFileLoader
    p = Path(__file__).resolve().parents[1] / "bin" / "nelix-reap"
    loader = SourceFileLoader("nelix_reap", str(p))
    spec = importlib.util.spec_from_loader("nelix_reap", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_reap_orphans_refuses_under_live_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    import paths; importlib.reload(paths)
    reap = _load_reap()
    monkeypatch.setattr(reap, "_any_per_gen_live", lambda: True)   # live
    out = reap.reap_orphans(inspector=None, killer=None, grace=5.0)
    assert out == {"refused": "daemon_alive"}


def test_reap_orphans_kills_when_daemon_dead(monkeypatch, tmp_path):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    import paths; importlib.reload(paths)
    from daemon import reaper
    sd = paths.sessions_root() / "s-orphan9"; sd.mkdir(parents=True)
    reaper.record_child(sd, {"sid": "s-orphan9", "daemon_pid": 10, "daemon_fingerprint": "d",
                             "pid": 999, "child_fingerprint": "c", "pgid": 999, "argv": ["x"]})
    reap = _load_reap()
    monkeypatch.setattr(reap, "_any_per_gen_live", lambda: False)                 # dead

    class _Insp:
        def is_alive(self, pid): return pid == 999
        def start_fingerprint(self, pid): return "c" if pid == 999 else "?"
        def pgid(self, pid): return 999
        def ppid(self, pid): return 1
    class _Killer:
        def __init__(self): self.calls = []
        def killpg(self, pgid, sig): self.calls.append((pgid, sig))
    killer = _Killer()
    out = reap.reap_orphans(inspector=_Insp(), killer=killer, grace=0.02)
    assert out["reaped"] == ["s-orphan9"]
    assert killer.calls                                  # killpg issued
