"""The acceptance gate for an INSTALLED core: build a wheel, install it into a clean venv, and run
the daemon out of that install.

Why this exists as a separate, expensive test: source-tree pytest cannot see any of what breaks on
install. It imports `daemon` from the repo, where `shim.wasm` sits next to `ghostty.py` and `bin/`
sits next to `daemon/` — so missing package data and repo-layout path derivation are invisible BY
CONSTRUCTION. Only a real wheel in a venv that has never seen the repo can fail the way a user
would.

The test is worthless if the repo leaks onto the installed interpreter's path, so it proves it did
not: `test_wheel_install_does_not_leak_the_repo` asserts the imported `daemon` lives under the venv,
and every probe runs with cwd OUTSIDE the repo, PYTHONPATH scrubbed, and no `-e` anywhere.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.slow


def _clean_env():
    """The installed interpreter must not be able to see the repo. Drop PYTHONPATH/PYTHONHOME and
    user site-packages; anything the probe imports has to come from the wheel."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run([str(c) for c in cmd], **kw)


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    """Build the wheel, install ONLY it into a clean 3.11 venv, and hand back that interpreter.

    Module-scoped: this costs a wheel build plus a venv, so it runs once and each guard below is a
    separate assertion against the same install.
    """
    tmp = tmp_path_factory.mktemp("real_wheel")
    dist, venv = tmp / "dist", tmp / "venv"
    env = _clean_env()

    build = _run(["uv", "build", "--wheel", "--out-dir", dist, REPO], cwd=tmp, env=env)
    assert build.returncode == 0, f"wheel build failed:\n{build.stdout}\n{build.stderr}"

    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"

    mk = _run(["uv", "venv", "--python", "3.11", venv], cwd=tmp, env=env)
    assert mk.returncode == 0, f"venv creation failed:\n{mk.stdout}\n{mk.stderr}"

    py = venv / "bin" / "python"
    # Only the wheel. No -e, no requirements.txt, no repo.
    inst = _run(["uv", "pip", "install", "--python", py, wheels[0]], cwd=tmp, env=env)
    assert inst.returncode == 0, f"wheel install failed:\n{inst.stdout}\n{inst.stderr}"
    return py, tmp


def _probe(installed, body):
    """Run `body` on the installed interpreter, cwd OUTSIDE the repo, and return its parsed JSON.

    The probe file itself is written outside the repo too, so `sys.path[0]` can never be a repo dir.
    """
    py, tmp = installed
    src = tmp / "probe.py"
    src.write_text(body)
    r = _run([py, src], cwd=tmp, env=_clean_env())
    assert r.returncode == 0, f"probe failed on the installed core:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_wheel_install_does_not_leak_the_repo(installed):
    """Guards the test, not the product: if the repo is importable from the install, every other
    assertion here is vacuous."""
    out = _probe(installed, """
import json, sys
import daemon, paths
print(json.dumps({"daemon": daemon.__file__, "paths": paths.__file__, "sys_path": sys.path}))
""")
    repo = str(REPO)
    assert "site-packages" in out["daemon"], f"daemon imported from {out['daemon']}, not the install"
    assert not out["daemon"].startswith(repo), f"the repo leaked: {out['daemon']}"
    assert not out["paths"].startswith(repo), f"the repo leaked: {out['paths']}"
    leaked = [p for p in out["sys_path"] if p.startswith(repo)]
    assert leaked == [], f"repo paths on the installed interpreter's sys.path: {leaked}"


def test_installed_core_imports_the_daemon(installed):
    """`daemon.app` is the daemon's entry module; it drags in most of the package (and `paths`,
    a TOP-LEVEL module the daemon imports — it has to ship too, or this dies at import)."""
    out = _probe(installed, """
import json
import daemon.app
from daemon.launchers import resolve_launcher
print(json.dumps({"ok": True, "app": daemon.app.__file__}))
""")
    assert out["ok"] is True


def test_installed_core_renders_through_packaged_wasm(installed):
    """The `shim.wasm` guard. `ghostty.py` loads the wasm relative to its own __file__, so if it
    does not ship as package data the install imports fine and dies the moment it renders."""
    out = _probe(installed, """
import json
from daemon.renderer.ghostty import GhosttyRenderer
r = GhosttyRenderer(cols=20, rows=3)
r.feed(b"hi")
f = r.snapshot()
r.close()
print(json.dumps({"first_row": f.rows[0], "cursor": list(f.cursor)}))
""")
    assert out["first_row"].startswith("hi"), f"packaged wasm rendered {out['first_row']!r}"
    assert out["cursor"] == [2, 0]


def test_installed_core_emits_hook_paths_that_exist(installed):
    """The console-script guard. `executor_message_instructions()` hands a worker ABSOLUTE paths to
    nelix-question/nelix-note. Derived from the repo layout, those paths simply do not exist once
    installed — and nothing says so: the worker just never uses the tool. Assert on the text the
    daemon actually emits, because that text is the contract."""
    out = _probe(installed, """
import json, os, re
from daemon.hook_settings import executor_message_instructions
text = executor_message_instructions()
found = re.findall(r"`(/[^\\s`]+/nelix-(?:question|note))\\b", text)
print(json.dumps({"paths": found, "text": text}))
""")
    paths = out["paths"]
    names = {Path(p).name for p in paths}
    assert names == {"nelix-question", "nelix-note"}, f"instructions named {paths}"
    for p in paths:
        assert os.path.isabs(p), f"{p} is not absolute (the executor's PATH is never touched)"
        assert os.path.exists(p), f"the daemon told a worker to run {p}, which does not exist"
        assert os.access(p, os.X_OK), f"{p} exists but is not executable"


def test_installed_hook_executables_actually_run(installed):
    """A path that exists and is +x can still be a broken console script (bad shebang, missing
    entry module). Run one: outside a nelix session it must exit 2 with its own diagnostic, which
    is `bin/nelix-question`'s documented contract."""
    py, tmp = installed
    out = _probe(installed, """
import json, re
from daemon.hook_settings import executor_message_instructions
text = executor_message_instructions()
q = re.findall(r"`(/[^\\s`]+/nelix-question)\\b", text)[0]
print(json.dumps({"q": q}))
""")
    env = {k: v for k, v in _clean_env().items()
           if k not in ("NELIX_HOOK_SOCK", "NELIX_HOOK_SECRET", "NELIX_SESSION")}
    r = _run([out["q"], "--question", "q", "--continuation-plan", "p"], cwd=tmp, env=env)
    assert r.returncode == 2, f"expected the not-in-a-session exit 2, got {r.returncode}: {r.stderr}"
    assert "not running under a nelix session" in r.stderr


def test_installed_core_does_not_drag_in_pytest(installed):
    """An installed core must not carry the test framework. Runtime deps under `daemon/` measured
    by AST on 2026-07-17: `wasmtime` and nothing else."""
    out = _probe(installed, """
import json, importlib.util
print(json.dumps({"pytest": importlib.util.find_spec("pytest") is not None,
                  "wasmtime": importlib.util.find_spec("wasmtime") is not None}))
""")
    assert out["wasmtime"] is True, "wasmtime is a real runtime dep and must install"
    assert out["pytest"] is False, "the wheel dragged pytest into a runtime install"
