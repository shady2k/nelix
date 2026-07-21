"""Immutable, version-addressed runtime installs — one directory per generation [nelix-9a4.2].

WHAT THIS IS FOR. `daemon/broker_client.py:31` spawns the PTY broker as
`[sys.executable, "-m", "daemon.pty_broker"]` and respawns it if it dies. A normal upgrade — the
kind that replaces files in place — therefore does something worse than restart the daemon: it
leaves generation N-1 running, and the first time N-1's broker dies, the respawn IMPORTS N'S CODE.
One live generation, two versions of the code, no error. "Old sessions keep running old code"
would be a slogan rather than a fact.

The fix is not to make the respawn cleverer. It is that `sys.executable` is version-addressed:
a generation's daemon runs `~/.nelix/runtimes/<build-id>/venv/bin/python`, that path contains
exactly one version of the code, and NOTHING EVER WRITES TO IT AGAIN. An upgrade cannot mix
versions because it cannot reach the old one — it builds a different directory and moves a
symlink. broker_client.py needs no change at all, which is the point.

    ~/.nelix/runtimes/
      <build-id>/
        python/         the generation's OWN interpreter (binary + stdlib)
        venv/           built from ../python; the core, wasmtime, shim.wasm, hook executables
        manifest.json   written LAST, atomically: the commit marker
      current -> <build-id>
      .install.lock

THREE THINGS HERE ARE MEASURED, NOT ASSUMED (2026-07-17, darwin 24.6.0) — each one is a way this
file could have looked correct and pinned nothing:

  * A VENV RETAINS NO INTERPRETER. `uv venv --python 3.11` symlinks `venv/bin/python` at
    `~/.local/share/uv/python/cpython-3.11-macos-aarch64-none` — an UNVERSIONED alias, itself a
    symlink to the current patch — and `sys.base_prefix`, hence the entire stdlib, resolves
    through it. `uv python install 3.11.16` would silently re-point every existing "immutable"
    runtime; `uv python uninstall` would break them all; the runtime directory would not change
    by a byte either way. `python -m venv --copies` does not fix it either: it copies the binary
    and leaves `home` (and `os.py`) in the shared store. So the interpreter is COPIED IN, and
    `venv/` is built from that copy. This is "uv tool install pins NOTHING by itself" [nelix-4el]
    arriving through the door the spec warned about.

  * A VENV CANNOT BE STAGED AND RENAMED. `pyvenv.cfg: home` is absolute and `bin/python` is a
    symlink into the interpreter home, so a venv built in a temp dir is dead the moment it is
    renamed — `bin/python` dangles. The tmp-then-`replace` trick that makes `_write_state`
    atomic does not scale to a tree. The build therefore happens AT the final path and commits
    by writing one small file (`manifest.json`) atomically at the end. A directory without a
    manifest is a partial install: never used, and rebuilt over.

  * `uv python find` IS ENVIRONMENT-SENSITIVE. Asked for `3.11.15` with this repo's own venv
    active it answers `.venv/bin/python3` — a venv, not a base interpreter. And `uv python list`
    reports three different providers of 3.11.15 (homebrew, a `~/.local/bin` shim, uv's store).
    "Provision the interpreter explicitly" therefore means: scrub `VIRTUAL_ENV`, demand
    `--managed-python`, ask for an exact patch, and then VERIFY the interpreter you got reports
    the version you asked for.

This module is deliberately NOT part of the wheel (like supervisor.py, and unlike paths.py): the
thing that installs runtimes lives outside them. `nelix daemon ensure` [nelix-3rm] is the caller
this is raw material for, and shipping it is that slice's call to make, not this one's.
"""
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

try:
    from . import paths
except ImportError:           # loaded as a top-level module (tests), not as a package
    import paths

# The EXACT patch every generation runs. Not a floor and not a range: two runtimes that differ
# only by interpreter patch are different runtimes, so this string is a build-id input. pyproject's
# `requires-python = "==3.11.*"` is the compatibility statement; this is the provisioning decision.
# Bumping it re-ids every runtime, which is correct — that is what a new interpreter means.
RUNTIME_PYTHON = "3.11.15"

RUNTIME_LOCK = Path(__file__).parent / "requirements-runtime.lock"

_BUILD_ID_HASH_LEN = 12       # 48 bits over a per-user directory of runtimes


class RuntimeInstallError(RuntimeError):
    pass


def _run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run([str(c) for c in cmd], **kw)


def _uv_env():
    """uv, told nothing about whatever environment happens to be active. VIRTUAL_ENV is what makes
    `uv python find 3.11.15` answer with the caller's venv instead of a base interpreter."""
    return {k: v for k, v in os.environ.items()
            if k not in ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "PYTHONHOME")}


# ---------------------------------------------------------------- identity

def wheel_digest(wheel) -> str:
    """A content digest of `wheel` that is stable across rebuilds of identical sources.

    NOT sha256 of the file, and the reason is narrower than it first looks — measured 2026-07-17,
    because the obvious version of this claim is false. Two `uv build`s of ONE tree are
    byte-identical, so within a checkout the file hash would do. But a zip stamps each member with
    its SOURCE FILE's mtime, and a fresh `git clone` (or the copytree the wheel tests do) writes
    new mtimes for identical code — so two builds of the same commit from two clones differ
    byte-for-byte. Keyed on the file, that mints a second build id, and therefore a second 78MB
    interpreter copy, for code that has not changed. Keyed on the payload — each member's name and
    bytes, sorted — it does not.
    """
    h = hashlib.sha256()
    with zipfile.ZipFile(wheel) as z:
        for name in sorted(z.namelist()):
            h.update(name.encode())
            h.update(b"\0")
            h.update(hashlib.sha256(z.read(name)).digest())
    return h.hexdigest()


def interpreter_id(python) -> str:
    """How the interpreter at `python` identifies itself — asked OF IT, not inferred from the path
    or the version we requested. A path can lie (`cpython-3.11-...` is an alias for whatever patch
    is current); an interpreter reporting its own `sys.version_info` cannot."""
    probe = ("import json,platform,sys;"
             "print(json.dumps({'v':'.'.join(map(str,sys.version_info[:3])),"
             "'impl':platform.python_implementation(),"
             "'machine':platform.machine(),'system':platform.system()}))")
    r = _run([python, "-c", probe], env=_uv_env())
    if r.returncode != 0:
        raise RuntimeInstallError(f"could not interrogate the interpreter at {python}:\n{r.stderr}")
    d = json.loads(r.stdout.strip().splitlines()[-1])
    return f"{d['impl'].lower()}-{d['v']}-{d['system'].lower()}-{d['machine'].lower()}"


def _core_version(wheel) -> str:
    """The wheel's own version, for a build id a human can read."""
    return Path(wheel).name.split("-")[1]


def build_id(wheel, interp_id: str, lock=RUNTIME_LOCK, *, extra_wheels=()) -> str:
    """The identity of everything that determines what code a generation runs: the core's payload,
    the FIRST-PARTY wheels shipped alongside it (nelix_store, nelix_contracts — nelix-m78), its
    pinned third-party closure, and the interpreter. Change any one and you have a different runtime
    that must live in a different directory — that is the whole mechanism.

    Content-addressed rather than sequential so `install()` is idempotent: installing the same
    inputs twice is a no-op on the second call rather than a second 78MB copy. The first-party wheels
    are folded in by their sorted `wheel_digest`, so the id does not depend on the order they were
    passed and an empty `extra_wheels` reproduces the pre-nelix-m78 id exactly.
    """
    h = hashlib.sha256()
    for part in (wheel_digest(wheel), Path(lock).read_bytes().decode(), interp_id):
        h.update(part.encode() if isinstance(part, str) else part)
        h.update(b"\0")
    for d in sorted(wheel_digest(ew) for ew in extra_wheels):
        h.update(d.encode())
        h.update(b"\0")
    return f"{_core_version(wheel)}-{h.hexdigest()[:_BUILD_ID_HASH_LEN]}"


# ---------------------------------------------------------------- provisioning

def provision_interpreter(version: str = RUNTIME_PYTHON) -> Path:
    """Install (idempotent) and locate the uv-managed CPython at EXACTLY `version`.

    `--managed-python` is not a nicety: `uv python list` shows three providers of 3.11.15 on this
    machine, and without it uv is free to answer with homebrew's — a python that an unrelated
    `brew upgrade` can move. The returned path is verified to report `version`, because the
    resolved store path is the one thing here allowed to be trusted only after it is checked.
    """
    env = _uv_env()
    inst = _run(["uv", "python", "install", "--managed-python", version], env=env)
    if inst.returncode != 0:
        raise RuntimeInstallError(
            f"could not provision CPython {version}:\n{inst.stdout}\n{inst.stderr}")
    found = _run(["uv", "python", "find", "--managed-python", version], env=env)
    if found.returncode != 0:
        raise RuntimeInstallError(
            f"provisioned CPython {version} but could not locate it:\n{found.stderr}")
    python = Path(found.stdout.strip().splitlines()[-1])
    got = interpreter_id(python)
    if f"-{version}-" not in got:
        raise RuntimeInstallError(
            f"asked uv for CPython {version} and got {got} at {python}")
    return python


def _base_prefix_of(python) -> Path:
    """The interpreter INSTALLATION `python` belongs to (`sys.base_prefix`), which is the tree that
    has to be copied — `python` itself is one symlink inside it, and the stdlib is the rest."""
    r = _run([python, "-c", "import sys; print(sys.base_prefix)"], env=_uv_env())
    if r.returncode != 0:
        raise RuntimeInstallError(f"could not read sys.base_prefix from {python}:\n{r.stderr}")
    return Path(r.stdout.strip().splitlines()[-1])


def _retain_interpreter(base: Path, dest: Path) -> None:
    """Give the generation its own copy of the interpreter tree — a REAL copy, ~78MB per
    generation, and every byte of it is load-bearing.

    Hardlinking the tree is the obvious optimisation (near-zero disk, and the inode outlives
    `uv python uninstall`) and it CAUSES THE EXACT BUG THIS MODULE EXISTS TO PREVENT. Measured
    2026-07-17: two runtimes whose `python/bin/python3.11` is one hardlinked inode report EACH
    OTHER's `sys.base_prefix` — generation A running generation B's stdlib, which is a
    version-mixed generation arriving through the floor. macOS resolves an executable back to a
    path via the kernel's inode->path cache, and a hardlink has no canonical path, so an inode
    with two names resolves to whichever one the cache happens to hold. It is not deterministic:
    it flipped depending on which tests ran first, and it passed in isolation.

    tests/test_real_runtime.py::test_generations_do_not_share_interpreter_inodes is the guard, and
    it is not theoretical — it is this bug, written down.

    So: distinct inodes per generation, non-negotiable. The price is 78MB and ~0.6s per install.
    """
    shutil.copytree(base, dest, symlinks=True)


# ---------------------------------------------------------------- install

def is_installed(build: str) -> bool:
    """True only for a COMMITTED runtime. The manifest is written last, so a directory without one
    is a partial install — an interrupted copy, or one still in progress."""
    return paths.runtime_manifest(build).is_file()


def install(wheel, *, python_version: str = RUNTIME_PYTHON, lock=RUNTIME_LOCK,
            extra_wheels=()) -> str:
    """Install `wheel` as an immutable runtime and return its build id. Idempotent: if that build
    id is already committed, this touches nothing and returns it.

    The install is frozen in the strong sense — an exact interpreter patch, retained; the core
    wheel by content; and its third-party closure by hash from `lock`. A plain `uv tool install`
    freezes none of the three.

    nelix-9a4.4: extra_wheels are local packages (nelix_store, nelix_contracts) that ship as
    separate wheels alongside the core. They are installed without hash pinning.
    """
    wheel = Path(wheel).resolve()
    lock = Path(lock).resolve()
    base_python = provision_interpreter(python_version)
    build = build_id(wheel, interpreter_id(base_python), lock, extra_wheels=extra_wheels)
    if is_installed(build):
        return build

    fd = _acquire_install_lock(paths.runtime_install_lock(), {"pid": os.getpid(), "build": build})
    if fd is None:
        raise RuntimeInstallError("another nelix runtime install is in progress")
    try:
        if is_installed(build):           # committed while we waited for the lock
            return build
        _build_at(paths.runtime_dir(build), wheel, lock, base_python, build, python_version,
                  extra_wheels)
        return build
    finally:
        os.close(fd)


def _acquire_install_lock(lock_path, meta: dict):
    """Exclusive, non-blocking flock over the runtime-install lock. Returns the open fd — the CALLER
    MUST KEEP IT for the duration, since closing it releases the lock — or None if another process
    holds it.

    This is deliberately a local stdlib copy of what daemon.singleton.acquire does, NOT an import of
    it: the same builder has to run inside a stdlib-only bootstrapper that has no daemon/ package at
    all. Same semantics, including non-blocking: a concurrent install is reported, never waited on
    (the bounded outer wait belongs to distribution.lock, a different lock, in a later slice).
    """
    paths.ensure_private_dir(os.path.dirname(lock_path) or ".")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(meta).encode())
    os.fsync(fd)
    return fd


def _build_at(rt: Path, wheel: Path, lock: Path, base_python: Path,
              build: str, python_version: str, extra_wheels=()) -> None:
    """Build the runtime AT its final path (a venv cannot be staged and renamed — see the module
    docstring) and commit it by writing the manifest atomically, last.
    nelix-9a4.4: extra_wheels are local packages installed alongside the core wheel."""
    shutil.rmtree(rt, ignore_errors=True)          # a previous partial install, if any
    paths.ensure_private_dir(rt)
    env = _uv_env()
    try:
        interp = paths.runtime_interpreter_home(build)
        _retain_interpreter(_base_prefix_of(base_python), interp)

        own_python = interp / "bin" / f"python{'.'.join(python_version.split('.')[:2])}"
        mk = _run([own_python, "-m", "venv", paths.runtime_dir(build) / "venv"], env=env)
        if mk.returncode != 0:
            raise RuntimeInstallError(f"venv creation failed:\n{mk.stdout}\n{mk.stderr}")

        py = paths.runtime_python(build)
        # Install local package wheels FIRST (no hash pinning — they ship with the core).
        # Must be before the core wheel so its dependencies resolve (nelix-store depends on
        # nelix-contracts). Install without --require-hashes since they're local.
        for ew in extra_wheels:
            ei = _run(["uv", "pip", "install", "--python", py, str(ew)],
                      env={**env, "VIRTUAL_ENV": str(paths.runtime_dir(build) / "venv")})
            if ei.returncode != 0:
                raise RuntimeInstallError(f"extra wheel install failed ({ew}):\n{ei.stdout}\n{ei.stderr}")

        # --require-hashes covers the whole third-party closure: the lock's pins by hash, and
        # the wheel by the digest of the bytes we were handed.
        reqs = rt / "requirements.txt"
        reqs.write_text(f"{lock.read_text()}\n"
                        f"{wheel} --hash=sha256:{_file_sha256(wheel)}\n")
        inst = _run(["uv", "pip", "install", "--python", py, "--require-hashes", "-r", reqs],
                    env={**env, "VIRTUAL_ENV": str(paths.runtime_dir(build) / "venv")})
        reqs.unlink()
        if inst.returncode != 0:
            raise RuntimeInstallError(f"runtime install failed:\n{inst.stdout}\n{inst.stderr}")

        _write_manifest(build, wheel, lock, base_python, python_version, extra_wheels)
    except BaseException:
        shutil.rmtree(rt, ignore_errors=True)      # never leave a half-built runtime behind
        raise


def _file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_manifest(build, wheel, lock, base_python, python_version, extra_wheels=()) -> None:
    """The commit. Written via tmp+replace so a runtime is never PARTLY committed: the manifest
    either exists whole or not at all. It records the FULL first-party closure's digests (nelix-m78)
    so the committed runtime is self-describing and a store/contracts change is visible."""
    m = paths.runtime_manifest(build)
    tmp = m.with_suffix(".json.tmp")
    body = json.dumps({
        "build_id": build,
        "core_version": _core_version(wheel),
        "wheel_digest": wheel_digest(wheel),
        "first_party_digests": {Path(ew).name: wheel_digest(ew) for ew in extra_wheels},
        "python_version": python_version,
        "interpreter_id": interpreter_id(paths.runtime_python(build)),
        "provisioned_from": str(base_python),
        "lock_sha256": _file_sha256(lock),
    }, indent=2, sort_keys=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    tmp.replace(m)


def read_manifest(build: str):
    try:
        return json.loads(paths.runtime_manifest(build).read_text())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- selection

def installed() -> list:
    """Committed runtimes, by build id. Partial installs are not runtimes and are not listed."""
    root = paths.runtimes_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and is_installed(p.name))


def activate(build: str) -> None:
    """Point `current` at `build`. The ONLY mutation an upgrade performs: one symlink swap, done
    atomically, touching no runtime. A daemon already running holds its own interpreter path and
    is unaffected — that is what makes this safe to do while generation N-1 is live."""
    if not is_installed(build):
        raise RuntimeInstallError(f"runtime {build} is not installed")
    link = paths.runtime_current()
    tmp = link.with_name("current.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(build)                          # relative: the runtimes root can move
    os.replace(tmp, link)


def active():
    """The build id new daemons should start from: `$NELIX_RUNTIME`, else `current`, else None.

    None means "no runtime installed" — the checkout, where the interpreter running this code is
    already the one with the core on it. It is NOT a fallback for a broken install: a NELIX_RUNTIME
    naming a runtime that is not committed is an error, not a reason to silently run other code.
    """
    env = os.environ.get("NELIX_RUNTIME", "").strip()
    if env:
        if not is_installed(env):
            raise RuntimeInstallError(
                f"NELIX_RUNTIME={env!r} names a runtime that is not installed under "
                f"{paths.runtimes_root()}")
        return env
    link = paths.runtime_current()
    if link.is_symlink():
        build = os.readlink(link)
        return build if is_installed(build) else None
    return None


def python_for(build: str) -> Path:
    """The interpreter of a committed runtime, checked to exist. Everything a daemon started with
    this spawns via sys.executable — the PTY broker especially — stays inside `build`."""
    py = paths.runtime_python(build)
    if not is_installed(build) or not py.exists():
        raise RuntimeInstallError(f"runtime {build} is not installed")
    return py


def active_python():
    """The active runtime's interpreter, or None in a checkout with no runtime installed."""
    build = active()
    return python_for(build) if build else None
