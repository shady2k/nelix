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
import shutil
import subprocess
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


# Build artifacts and environments, never build INPUTS. `build/` matters most: setuptools' build_py
# copies sources into `build/lib/` and never prunes stale entries, so a repo that was built once
# with `shim.wasm` as package data keeps emitting wheels containing `shim.wasm` even after the
# declaration is deleted -- the wheel is assembled from the leftovers. Building a fresh copy is what
# makes this test's answers about the wheel true rather than historical.
_NOT_SOURCE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "build", "dist", "*.egg-info", "__pycache__", "*.py[cod]",
    ".pytest_cache", "spikes",
)


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    """Build the wheel from a pristine copy of the tree, install ONLY it into a clean 3.11 venv,
    and hand back that interpreter.

    Module-scoped: this costs a wheel build plus a venv, so it runs once and each guard below is a
    separate assertion against the same install.
    """
    tmp = tmp_path_factory.mktemp("real_wheel")
    src, dist, venv = tmp / "src", tmp / "dist", tmp / "venv"
    env = _clean_env()

    # A fresh clone of the working tree: no stale build/, and the repo is left untouched (building
    # in place would litter it with build/ + *.egg-info).
    shutil.copytree(REPO, src, ignore=_NOT_SOURCE, symlinks=True)
    assert not (src / "build").exists(), "stale build/ leaked into the pristine copy"

    build = _run(["uv", "build", "--wheel", "--out-dir", dist, src], cwd=tmp, env=env)
    assert build.returncode == 0, f"wheel build failed:\n{build.stdout}\n{build.stderr}"

    # nelix-9a4.4: nelix_store and nelix_contracts are now runtime deps of the core.
    # Build their wheels too so the installed core can find them.
    for pkg in ("packages/nelix_contracts", "packages/nelix_store"):
        b = _run(["uv", "build", "--wheel", "--out-dir", dist, src / pkg], cwd=tmp, env=env)
        assert b.returncode == 0, f"{pkg} wheel build failed:\n{b.stdout}\n{b.stderr}"

    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 3, f"expected 3 wheels (core + nelix_contracts + nelix_store), got {wheels}"

    mk = _run(["uv", "venv", "--python", "3.11", venv], cwd=tmp, env=env)
    assert mk.returncode == 0, f"venv creation failed:\n{mk.stdout}\n{mk.stderr}"

    py = venv / "bin" / "python"
    # Install all three wheels (core + nelix_contracts + nelix_store).
    # No -e, no requirements.txt, no repo.
    inst = _run(["uv", "pip", "install", "--python", py, *wheels], cwd=tmp, env=env)
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


def test_installed_core_spawns_and_drives_a_real_pty(installed):
    """"It runs" at its strongest: the installed core stands up its own PTY broker as a subprocess
    and drives a real child through the master fd.

    tests/test_pty_broker.py drives the same path but launches the broker with `cwd=repo` and
    `PYTHONPATH=repo` -- from an install there is neither, so `python -m daemon.pty_broker` has to
    resolve out of site-packages on its own. This is also what retires `ptyprocess` honestly: the
    PTY comes up in a venv where it is not installed, because the broker uses stdlib os.openpty +
    os.login_tty (daemon/pty_broker.py:109,57), not ptyprocess.
    """
    out = _probe(installed, r"""
import json, os, subprocess, sys, time, importlib.util
from daemon.broker_proto import send_msg, recv_msg, make_socketpair

daemon_end, broker_end = make_socketpair()
os.set_inheritable(broker_end.fileno(), True)
# No PYTHONPATH, no cwd=repo: the installed broker must find itself.
proc = subprocess.Popen([sys.executable, "-m", "daemon.pty_broker", str(broker_end.fileno())],
                        pass_fds=[broker_end.fileno()])
broker_end.close()
try:
    send_msg(daemon_end, {"v": 1, "argv": ["cat"], "cwd": "/", "env": dict(os.environ),
                          "cols": 80, "rows": 24})
    resp, master = recv_msg(daemon_end)
    echoed = b""
    if resp.get("status") == "ok":
        os.write(master, b"hi\n")
        deadline = time.time() + 5
        while b"hi" not in echoed and time.time() < deadline:
            time.sleep(0.05)
            echoed += os.read(master, 4096)
        os.close(master)
        os.kill(resp["pid"], 9)
    print(json.dumps({"status": resp.get("status"), "echoed": b"hi" in echoed,
                      "own_group_leader": resp.get("pid") == resp.get("pgid"),
                      "ptyprocess_installed": importlib.util.find_spec("ptyprocess") is not None}))
finally:
    daemon_end.close(); proc.terminate(); proc.wait(timeout=5)
""")
    assert out["status"] == "ok", f"the installed broker could not spawn: {out}"
    assert out["echoed"] is True, "the master fd did not drive the child"
    assert out["own_group_leader"] is True
    assert out["ptyprocess_installed"] is False, \
        "ptyprocess got into the install; the PTY above proves the core does not need it"


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


def test_installed_core_imports_the_router_and_the_cli(installed):
    out = _probe(installed, """
import json
import router.app
import nelix_cli.cli
import rpc_client, runtime, generation_supervisor, paths, launcher_resolve
print(json.dumps({"router": router.app.__file__, "cli": nelix_cli.cli.__file__}))
""")
    assert "site-packages" in out["router"], f"router imported from {out['router']}, not the install"
    assert "site-packages" in out["cli"], f"nelix_cli imported from {out['cli']}, not the install"
    assert not out["router"].startswith(str(REPO)), f"the repo leaked: {out['router']}"


def test_installed_core_puts_nelix_on_the_path(installed):
    py, tmp = installed
    exe = py.parent / "nelix"
    assert exe.exists(), "the wheel must install a nelix console script"

    r = _run([exe, "--help"], cwd=tmp, env=_clean_env())
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    for verb in ("daemon", "rpc", "wait", "config"):
        assert verb in r.stdout, f"`nelix --help` does not offer {verb}: {r.stdout}"


def test_installed_nelix_answers_the_cli_api_envelope(installed):
    py, tmp = installed
    home = tmp / "cli_home"
    home.mkdir(exist_ok=True)

    r = _run([py.parent / "nelix", "rpc", "status", "--owner", "harness-x"],
             cwd=tmp, env={**_clean_env(), "NELIX_HOME": str(home)})

    assert r.returncode == 3, f"expected the unavailable exit class, got {r.returncode}: {r.stderr}"
    body = json.loads(r.stdout.strip().splitlines()[-1])
    assert body["cli_api"] == 1
    assert body["ok"] is False
    assert body["error"]["code"] == "router_unavailable"
    assert "Traceback" not in r.stderr
