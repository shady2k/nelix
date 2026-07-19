"""The acceptance gate for an INSTALLED RUNTIME [nelix-9a4.2]: build two real generations from two
real wheels and prove the older one cannot be dragged onto the newer one's code.

This is the test the slice exists for. `daemon/broker_client.py:31` respawns a dead PTY broker with
`[sys.executable, "-m", "daemon.pty_broker"]`. Under an in-place upgrade that is a version-mixed
generation: N-1 keeps running, its broker dies, and the respawn imports N's code — silently, with
no error anywhere. Nothing in the source tree can catch it, because in a checkout there is only
ever one version of the code on disk. It takes two installed generations, side by side, with the
newer one ACTIVE, to ask the question at all.

Expensive on purpose (two wheel builds, two interpreter copies, two dependency installs) and marked
slow. The cheap half — build ids, commit semantics, what the supervisor does with an active runtime
— is tests/test_runtime.py.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402
import runtime  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.slow

# Same reasoning as tests/test_real_wheel.py: build artifacts and environments, never build INPUTS.
# setuptools' build_py never prunes build/lib, so a tree that was built once keeps emitting what it
# built then, regardless of what pyproject says now.
_NOT_SOURCE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "build", "dist", "*.egg-info", "__pycache__", "*.py[cod]",
    ".pytest_cache", "spikes",
)


def _run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run([str(c) for c in cmd], **kw)


def _build_wheel(src: Path, out: Path, version: str) -> Path:
    """Build a wheel of the tree at `src`, stamped `version`. The version is the generation marker:
    the installed core reports it via importlib.metadata, so a probe can say WHICH generation's code
    it is running without the test having to plant anything artificial in the source."""
    pp = src / "pyproject.toml"
    pp.write_text(pp.read_text().replace('version = "0.1.0"', f'version = "{version}"', 1))
    r = _run(["uv", "build", "--wheel", "--out-dir", out, src], cwd=src.parent)
    assert r.returncode == 0, f"wheel build failed:\n{r.stdout}\n{r.stderr}"
    wheels = [w for w in out.glob(f"*-{version}-*.whl")]
    assert len(wheels) == 1, f"expected one {version} wheel, got {wheels}"
    return wheels[0]


@pytest.fixture(scope="module")
def generations(tmp_path_factory):
    """Two installed generations in one NELIX_HOME, with the NEWER one active — an upgrade, exactly
    as a user would have it after `nelix runtime install` twice.

    Returns (home, build_a, build_b): 0.1.0 is generation A (the one already running sessions),
    0.2.0 is generation B (the upgrade).
    """
    tmp = tmp_path_factory.mktemp("real_runtime")
    home, src, dist = tmp / "home", tmp / "src", tmp / "dist"
    shutil.copytree(REPO, src, ignore=_NOT_SOURCE, symlinks=True)

    old = os.environ.get("NELIX_HOME")
    os.environ["NELIX_HOME"] = str(home)
    os.environ.pop("NELIX_RUNTIME", None)
    try:
        # nelix-9a4.4: build + install local packages alongside the core wheel
        _build_local = lambda pkg: _run(
            ["uv", "build", "--wheel", "--out-dir", dist, src / "packages" / pkg],
            cwd=tmp).returncode == 0
        _find = lambda name: next(iter(dist.glob(f"{name}*.whl")))
        extra = []
        for pkg, name in [("nelix_contracts", "nelix_contracts"),
                          ("nelix_store", "nelix_store")]:
            assert _build_local(pkg), f"{name} wheel build failed"
            extra.append(_find(name))
        build_a = runtime.install(_build_wheel(src, dist, "0.1.0"), lock=src / RUNTIME_LOCK_NAME,
                                   extra_wheels=extra)
        # Rebuild for 0.2.0 (different version stamp)
        extra2 = []
        for pkg, name in [("nelix_contracts", "nelix_contracts"),
                          ("nelix_store", "nelix_store")]:
            extra2.append(_find(name))
        build_b = runtime.install(_build_wheel(src, dist, "0.2.0"), lock=src / RUNTIME_LOCK_NAME,
                                   extra_wheels=extra2)
        runtime.activate(build_b)                       # the upgrade lands; A is still live
        yield home, build_a, build_b
    finally:
        if old is None:
            os.environ.pop("NELIX_HOME", None)
        else:
            os.environ["NELIX_HOME"] = old


RUNTIME_LOCK_NAME = "requirements-runtime.lock"


@pytest.fixture(autouse=True)
def _use_generations_home(isolate_nelix_home, generations, monkeypatch):
    """Re-point NELIX_HOME at the two generations for the duration of each test.

    conftest's `isolate_nelix_home` is autouse and function-scoped, so it hands every test a FRESH
    empty root — correct for the rest of the suite, and it would otherwise hide the runtimes this
    module spent two wheel builds installing. Depending on it explicitly pins the ordering rather
    than relying on it.
    """
    monkeypatch.setenv("NELIX_HOME", str(generations[0]))
    monkeypatch.delenv("NELIX_RUNTIME", raising=False)


def _probe(python, body):
    """Run `body` on a generation's interpreter with the repo scrubbed off the environment, and
    return its parsed JSON. cwd is outside the repo and PYTHONPATH is gone, so anything the probe
    imports came from the runtime — otherwise every assertion below would be vacuous."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONNOUSERSITE"] = "1"
    r = _run([python, "-c", body], cwd=str(Path(python).parent), env=env)
    assert r.returncode == 0, f"probe failed on {python}:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


_WHO_AM_I = """
import json, importlib.metadata, daemon, sys
print(json.dumps({"version": importlib.metadata.version("nelix-core"),
                  "daemon": daemon.__file__, "base_prefix": sys.base_prefix,
                  "executable": sys.executable}))
"""


# ---------------------------------------------------------------- the hazard

def test_a_generations_broker_respawn_stays_in_its_own_generation(generations):
    """THE test. Generation A re-spawns its broker exactly the way broker_client.py does — via
    `sys.executable` — while generation B is installed and `current` points at B.

    If the runtime were mutable (an in-place upgrade) or if the daemon ran from a shared
    interpreter, the child here would come up on B's code inside A's session. That is the
    version-mixed generation, and it is silent: no import error, no version check, nothing. The
    child must report A.
    """
    home, build_a, build_b = generations
    assert runtime.active() == build_b, "fixture precondition: the NEWER generation is active"

    child = _probe(runtime.python_for(build_a), f"""
import json, subprocess, sys
# Precisely broker_client.py's spawn: whatever sys.executable is, with no path help.
r = subprocess.run([sys.executable, "-c", {_WHO_AM_I!r}], capture_output=True, text=True)
print(r.stdout.strip().splitlines()[-1])
""")
    assert child["version"] == "0.1.0", (
        f"generation {build_a} respawned onto {child['version']} code — a version-mixed generation")
    assert child["daemon"].startswith(str(paths.runtime_dir(build_a))), \
        f"the respawn imported daemon from {child['daemon']}, outside its own generation"
    assert not child["daemon"].startswith(str(REPO)), f"the checkout leaked in: {child['daemon']}"


def test_the_real_pty_broker_comes_up_inside_the_old_generation(generations):
    """The same claim through the actual broker rather than a stand-in: generation A stands up
    `daemon.pty_broker` as a subprocess, drives a real child through the master fd, and the broker
    module it loaded is A's — with B installed and active."""
    home, build_a, build_b = generations
    out = _probe(runtime.python_for(build_a), r"""
import json, os, subprocess, sys, time
from daemon.broker_proto import send_msg, recv_msg, make_socketpair
daemon_end, broker_end = make_socketpair()
os.set_inheritable(broker_end.fileno(), True)
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
        os.close(master); os.kill(resp["pid"], 9)
    import daemon.pty_broker as b
    print(json.dumps({"status": resp.get("status"), "echoed": b"hi" in echoed,
                      "broker": b.__file__}))
finally:
    daemon_end.close(); proc.terminate(); proc.wait(timeout=5)
""")
    assert out["status"] == "ok", f"the old generation's broker could not spawn: {out}"
    assert out["echoed"] is True, "the master fd did not drive the child"
    assert out["broker"].startswith(str(paths.runtime_dir(build_a))), \
        f"the broker loaded from {out['broker']}, outside generation {build_a}"


def test_installing_and_activating_a_new_generation_does_not_touch_the_old_one(generations):
    """Immutability, asserted on bytes rather than intent: every file of A is unchanged after B was
    built and `current` moved. An upgrade that writes into a live generation is the whole bug."""
    home, build_a, build_b = generations
    rt = paths.runtime_dir(build_a)
    digest = {str(p.relative_to(rt)): (p.stat().st_size, p.stat().st_mtime_ns)
              for p in sorted(rt.rglob("*")) if p.is_file() and not p.is_symlink()}
    assert digest, "generation A has no files"
    before = json.loads(paths.runtime_manifest(build_a).read_text())

    runtime.activate(build_a)
    runtime.activate(build_b)                            # flip back and forth

    after = {str(p.relative_to(rt)): (p.stat().st_size, p.stat().st_mtime_ns)
             for p in sorted(rt.rglob("*")) if p.is_file() and not p.is_symlink()}
    assert after == digest, "activating another generation modified generation A's files"
    assert json.loads(paths.runtime_manifest(build_a).read_text()) == before


def test_the_two_generations_run_different_code(generations):
    """The fixture's premise. If both wheels installed the same version this whole file would pass
    vacuously — A could 'stay on its own code' by there being only one."""
    home, build_a, build_b = generations
    assert build_a != build_b, "two different cores got the same build id"
    assert _probe(runtime.python_for(build_a), _WHO_AM_I)["version"] == "0.1.0"
    assert _probe(runtime.python_for(build_b), _WHO_AM_I)["version"] == "0.2.0"


# ---------------------------------------------------------------- what a runtime retains

def test_a_generation_owns_its_interpreter(generations):
    """`sys.base_prefix` — the stdlib's home — must be INSIDE the generation.

    The natural way to write this installer is `uv venv --python 3.11`, and it would look right and
    pin nothing: that symlinks venv/bin/python at the unversioned alias
    `~/.local/share/uv/python/cpython-3.11-macos-aarch64-none`, which is itself a symlink to the
    current patch. `uv python install 3.11.16` would then re-point every 'immutable' runtime at a
    new interpreter and `uv python uninstall` would break them all, without a byte of the runtime
    changing. `python -m venv --copies` does not fix it either — it copies the binary and leaves
    the stdlib in the shared store (measured 2026-07-17).
    """
    home, build_a, _ = generations
    py = runtime.python_for(build_a)
    out = _probe(py, _WHO_AM_I)
    rt = str(paths.runtime_dir(build_a))
    assert out["base_prefix"].startswith(rt), (
        f"the generation's stdlib lives at {out['base_prefix']}, outside the runtime — an "
        f"interpreter it does not own can be changed or removed underneath it "
        f"(probed {py}, which reported executable={out['executable']})")
    assert out["executable"].startswith(rt), f"probed {py}, got {out['executable']}"
    stdlib = _probe(runtime.python_for(build_a),
                    "import json, os; print(json.dumps({'os': os.__file__}))")
    assert stdlib["os"].startswith(rt), f"os.py loads from {stdlib['os']}, outside the runtime"


def test_generations_do_not_share_interpreter_inodes(generations):
    """Two generations must not share one interpreter FILE, however identical the bytes.

    This is a real bug, written down. The first version of _retain_interpreter hardlinked the
    interpreter tree — near-zero disk, and the inode survives `uv python uninstall`, so it looked
    strictly better. It made generation A report generation B's `sys.base_prefix`: macOS resolves
    an executable back to a path through the kernel's inode->path cache, and a hardlinked inode has
    no canonical path, so one with two names resolves to whichever name the cache holds. A ran B's
    stdlib — the version-mixed generation, arriving underneath the mechanism meant to prevent it.

    test_a_generation_owns_its_interpreter caught it only by luck of ordering: it passed in
    isolation and failed once tests/test_real_wheel.py ran first. This asserts the underlying
    property instead, so a re-introduction fails every time rather than one run in three.
    """
    home, build_a, build_b = generations
    a = paths.runtime_interpreter_home(build_a) / "bin" / "python3.11"
    b = paths.runtime_interpreter_home(build_b) / "bin" / "python3.11"
    assert a.stat().st_ino != b.stat().st_ino, (
        "two generations share one interpreter inode; macOS will resolve it back to whichever "
        "path its cache holds, and a generation will silently run the other's stdlib")
    assert a.stat().st_dev == b.stat().st_dev     # same fs: the inodes are comparable at all


def test_a_generation_ships_the_wasm_and_renders(generations):
    """shim.wasm is loaded relative to ghostty.py's __file__: a runtime missing it imports fine and
    dies the first time a session renders."""
    home, build_a, _ = generations
    out = _probe(runtime.python_for(build_a), """
import json
from daemon.renderer.ghostty import GhosttyRenderer
r = GhosttyRenderer(cols=20, rows=3)
r.feed(b"hi"); f = r.snapshot(); r.close()
print(json.dumps({"row": f.rows[0], "wasmtime": __import__("wasmtime").__file__}))
""")
    assert out["row"].startswith("hi")
    assert out["wasmtime"].startswith(str(paths.runtime_dir(build_a))), \
        "the generation renders through a wasmtime from outside its own frozen closure"


def test_a_generation_carries_its_hook_executables(generations):
    """hook_settings hands these absolute paths to every hook-capable executor. They must live in
    the generation, so a worker launched by an old session keeps reaching the old tools."""
    home, build_a, _ = generations
    out = _probe(runtime.python_for(build_a), """
import json, re
from daemon.hook_settings import executor_message_instructions
print(json.dumps({"paths": re.findall(r"`(/[^\\s`]+/nelix-(?:question|note))\\b",
                                      executor_message_instructions())}))
""")
    found = out["paths"]
    assert {Path(p).name for p in found} == {"nelix-question", "nelix-note"}, found
    for p in found:
        assert os.access(p, os.X_OK), f"{p} is not executable"
        assert p.startswith(str(paths.runtime_dir(build_a))), \
            f"the generation points its workers at {p}, outside itself"


def test_a_generation_does_not_carry_the_dev_extras(generations):
    """A frozen runtime is the runtime closure and nothing else — and ptyprocess's absence is what
    keeps the retired claim honest: the PTY above came up without it."""
    home, build_a, _ = generations
    out = _probe(runtime.python_for(build_a), """
import json, importlib.util
print(json.dumps({m: importlib.util.find_spec(m) is not None
                  for m in ("pytest", "yaml", "ptyprocess", "wasmtime")}))
""")
    assert out["wasmtime"] is True
    for absent in ("pytest", "yaml", "ptyprocess"):
        assert out[absent] is False, f"{absent} got frozen into a generation"


# ---------------------------------------------------------------- install behaviour

def test_installing_the_same_inputs_twice_is_a_no_op(generations):
    """Content-addressed, so a re-install of unchanged code must not mint a second generation — nor
    rewrite the first, which is live."""
    home, build_a, _ = generations
    wheel = next((home.parent / "dist").glob("*-0.1.0-*.whl"))
    manifest = paths.runtime_manifest(build_a)
    before = manifest.stat().st_mtime_ns
    again = runtime.install(wheel, lock=REPO / RUNTIME_LOCK_NAME)
    assert again == build_a
    assert manifest.stat().st_mtime_ns == before, "a re-install rewrote a live generation"
    assert sorted(runtime.installed()) == sorted(set(runtime.installed()))


def test_the_manifest_records_what_was_frozen(generations):
    home, build_a, _ = generations
    m = runtime.read_manifest(build_a)
    assert m["build_id"] == build_a
    assert m["core_version"] == "0.1.0"
    assert m["python_version"] == runtime.RUNTIME_PYTHON
    assert runtime.RUNTIME_PYTHON in m["interpreter_id"], m["interpreter_id"]
