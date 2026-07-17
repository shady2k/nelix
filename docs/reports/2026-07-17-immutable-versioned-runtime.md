# Immutable versioned runtime install [nelix-9a4.2] — worker report

**Branch:** `shady2k/nelix_runtime` on `main` 8cea394. **Not pushed.**

**Result:** the core installs as an immutable, version-addressed runtime under
`~/.nelix/runtimes/<build-id>/`, and two generations of it live side by side without touching each
other. The hazard the slice exists for is closed and measured: **generation A re-spawns its PTY
broker exactly the way `broker_client.py:31` does, with generation B installed and `current`
pointing at B, and comes up on A's code** — through the real `daemon.pty_broker`, driving a real
PTY.

| measurement | number |
|---|---|
| `make test` | **1339 passed, 3 skipped** |
| baseline (re-measured at base, this work stashed) | **1315 passed, 3 skipped** — confirmed |
| delta | **+24** (35 added, 11 deleted) |
| mutations applied / caught | **13 / 13** |

Arithmetic: 1315 + 35 (21 `test_runtime.py`, 3 `test_runtime_lock.py`, 11 `test_real_runtime.py`)
− 11 (the deps-hack tests, whose subject no longer exists) = 1339.

**The base is intermittently red and it is not mine.** `tests/test_nelix_wait.py::
test_nelix_wait_graceful_interrupt` failed with an `OSError` on one of three base runs with my work
stashed, and passed on the other two. Pre-existing, in code I did not touch, not fixed here — but
the brief's "baseline: 1315 passed, 3 skipped" is only true about 2 runs in 3. Worth a bead.

## I shipped the exact bug this slice exists to prevent, and the test caught it

`_retain_interpreter` first **hardlinked** the interpreter tree into each runtime. It looked
strictly better than copying: near-zero disk, and the inode survives `uv python uninstall`, so a
generation would outlive its own interpreter being removed.

It made **generation A report generation B's `sys.base_prefix`** — A running B's stdlib. A
version-mixed generation, arriving underneath the mechanism built to prevent it. Reproduced in
isolation, and the symptom is unambiguous:

```
genA/venv → base_prefix = .../genB/python
genB/venv → base_prefix = .../genA/python     # they swapped
```

macOS resolves an executable back to a path through the kernel's inode→path cache. A hardlinked
inode has no canonical path, so an inode with two names resolves to **whichever name the cache
happens to hold**. Both runtimes' `python/bin/python3.11` were one inode (57869033), and each venv
found the other's home.

It was not deterministic. It **passed in isolation and failed only once
`tests/test_real_wheel.py` ran first** — I chased it through three clean runs believing it was a
flake before it reproduced. `test_a_generation_owns_its_interpreter` caught it by luck of ordering;
`test_generations_do_not_share_interpreter_inodes` now asserts the underlying property (distinct
inodes) so a re-introduction fails every time. The price of the fix is real and stated: a **78MB
copy per generation, ~0.6s**.

## The plan was evidence, not scripture: what measured false

You predicted an eleventh disproof. Three, and one of them was my own.

**1. A venv retains NO interpreter — the spec's own hazard, one level lower.** "layout
`<build-id>/venv` retaining interpreter" reads like `uv venv --python 3.11` satisfies it. It does
not, and the way it fails is precisely `nelix-cb0` coming back: `uv venv` symlinks `venv/bin/python`
at `~/.local/share/uv/python/cpython-3.11-macos-aarch64-none` — an **unversioned alias that is
itself a symlink to the current patch** — and `sys.base_prefix`, hence the whole stdlib, resolves
through it. `uv python install 3.11.16` would silently re-point **every existing "immutable"
runtime**, and `uv python uninstall` would break them all, **without one byte of the runtime
directory changing**. `python -m venv --copies` does not fix it either: it copies the 17MB binary
and leaves `home` — and `os.py` — in the shared store. Hence `<build-id>/python/`: the generation
owns its interpreter, or it owns nothing.

**2. A venv cannot be staged and renamed into place.** The repo's atomicity idiom is tmp-then-
`replace` (`_write_state`), and it does not scale to a tree: `pyvenv.cfg: home` is absolute and
`bin/python` is a symlink into the interpreter home, so a staged venv is dead on arrival —
`bin/python` dangles after the rename. The build therefore happens **at the final path** and commits
by writing one small file (`manifest.json`) atomically, last. A directory without a manifest is a
partial install: never used, rebuilt over.

**3. `uv python find` is environment-sensitive — "pins nothing by itself" has a third door.** Asked
for `3.11.15` with this repo's own venv active, it answers **`.venv/bin/python3`** — a venv, not a
base interpreter. And `uv python list` reports **three** providers of 3.11.15 here (homebrew, a
`~/.local/bin` shim, uv's store). Provisioning explicitly therefore means: scrub `VIRTUAL_ENV`,
demand `--managed-python`, ask for an exact patch, and **verify the interpreter you got reports the
version you asked for**.

**4. My own docstring was false, and the mutation matrix caught it.** I wrote that `wheel_digest`
hashes the payload because "setuptools stamps timestamps, so two builds of one tree differ
byte-for-byte." Mutation 5 (key the build id on the file's sha256) came back **GREEN**. Two
`uv build`s of one tree are **byte-identical** — the claim was simply wrong. The real mechanism is
narrower: a zip stamps each member with its **source file's mtime**, so a fresh `git clone` (or the
copytree the wheel tests do) yields a different wheel for identical code — measured: two fresh
copies → file sha256 **differs**, payload digest **identical**. The conclusion survived; the
reasoning did not. And the test was reading its own fake: `writestr` with a bare name stamps every
member 1980-01-01, so both wheels came out byte-identical and the assertion could not fail.

## Decisions you asked me to make and justify

**`requirements-daemon.lock` → `requirements-runtime.lock`, COMPILED FROM `pyproject.toml`.** The
old lock's *contents* were wrong (it pinned `ptyprocess`, which nothing under `daemon/` has ever
imported) and its only consumer — the deps hack — dies here. But the *concept* is what the bead
demands. So it is regenerated as the closure every generation is frozen from, and compiled from the
package's own `[project.dependencies]` rather than a hand-kept `.in`. That is the same move that put
`-e .` in `requirements.txt`: **the two cannot drift because one is derived from the other.**
`requirements-daemon.in` is deleted — a hand-kept input is exactly how the ptyprocess fiction
survived. `tests/test_runtime_lock.py` fails if the lock and pyproject disagree, if a pin lacks
hashes, or if a retired dep returns.

**What survives of the spawn + deps hack.**

- **Dies:** `_ensure_deps`, `_venv_pip_install`, `_deps_present`, `_lazy_installs_allowed`,
  `_resolve_uv`, `_DAEMON_DEPS`, `_DAEMON_MODULES`, `_DAEMON_LOCK`, and 11 tests. The hack existed
  to make wasmtime importable in an interpreter that could `import daemon.app` **without having
  installed the core**. After 9a4.1 there is no such interpreter: installing the core brings
  wasmtime with it, so "has the code" and "has the deps" are one act. A generation gets both frozen
  in; a checkout gets both from `-e .`. No third case remains. It also had a core module importing
  `hermes_cli.config` to ask **a harness for permission to install its own dependencies** — and an
  immutable runtime forbids the operation outright. The module docstring records why, so it is not
  reinvented.
- **Survives:** `ensure_running` / `_daemon_argv`, still raw material for `nelix daemon ensure`
  [nelix-3rm]. `_daemon_argv` is now **the pin point**: the interpreter it picks becomes the
  daemon's `sys.executable`, which `broker_client.py` respawns with — so pinning it pins the
  generation for life. **`broker_client.py` is unchanged, and that is the point**: the fix is not a
  cleverer respawn, it is that `sys.executable` is version-addressed.

**A leak I had to close to make any of it true.** `ensure_running` put `PLUGIN_ROOT` on the child's
`PYTHONPATH` and used `cwd=PLUGIN_ROOT` unconditionally. **PYTHONPATH precedes site-packages**, so a
runtime-launched daemon would have imported the **working tree**, and the version-addressed
directory would have bought nothing. A runtime now gets `PYTHONPATH`/`PYTHONHOME` **scrubbed** (not
merely un-injected — an inherited one leaks a repo in just as well) plus `PYTHONNOUSERSITE`, and a
neutral cwd. A checkout keeps the injection, because there the checkout **is** the install.

**`runtime.py` does not ship**, like `supervisor.py` and unlike `paths.py`: the thing that installs
runtimes lives outside them. Shipping it is `nelix-3rm`'s call, when there is a `nelix` CLI to hang
it on. **`RUNTIME_PYTHON = "3.11.15"`** is an exact patch and a build-id input, not a floor —
bumping it re-ids every runtime, which is what a new interpreter means.

**Selection is minimal on purpose.** `active()` = `$NELIX_RUNTIME`, else the `current` symlink, else
None (the checkout). An upgrade's only mutation is one atomic symlink swap. A `NELIX_RUNTIME` naming
an uninstalled runtime **raises** rather than falling back — being asked for a specific generation
and quietly running other code is the failure this slice exists to make impossible. If `nelix-3rm`'s
router wants a different pointer, this is one symlink to replace.

## Detection power: 13 mutations, each alone, each red (control: 63 passed)

| # | mutation | red |
|---|---|---|
| 1 | retain the interpreter by hardlink (**the bug I shipped**) | 1 — `generations_do_not_share_interpreter_inodes` |
| 2 | do not retain the interpreter (venv off the shared store) | 2 — `owns_its_interpreter`, +1 |
| 3 | build id ignores the interpreter | 1 |
| 4 | build id ignores the locked closure | 1 |
| 5 | build id keyed on the wheel FILE, not its payload | 1 |
| 6 | a directory counts as installed (no manifest commit) | 4 |
| 7 | a missing `NELIX_RUNTIME` falls back instead of raising | 1 |
| 8 | daemon spawns from `sys.executable` regardless of runtime | 1 |
| 9 | the checkout on every daemon's PYTHONPATH (pre-9a4.2) | 1 |
| 10 | `ptyprocess` back in the runtime lock | 1 |
| 11 | the lock loses its hashes | 1 |
| 12 | install is not idempotent (rebuilds a live generation) | 1 |
| 13 | `activate()` re-points to an uncommitted runtime | 1 |

No guard here is a diagnostic. **Three came back green first and two of those were my mutations
being wrong, not the guards being inert** — #11 commented a backslash and left the hash lines
readable, and #12 removed the first idempotency check while the second, inside the install lock,
still short-circuited. #5 was the real one, and it was a test reading its own fake (above). Reported
because the difference between "the guard is inert" and "my mutation was weak" is exactly what the
`_CORRUPT_CODES` precedent is about, and it is only visible if you check.

## Loose ends I did not touch

- **`--require-hashes` at install time has no guard.** Mutation 11 proves the *lock* must carry
  hashes; nothing fails if `_build_at` stops passing `--require-hashes`. Testing it honestly needs a
  tampered index, which is more than this slice.
- **The PYTHONPATH-leak guard is unit-only.** `test_a_runtime_daemon_does_not_get_the_checkout_on_
  its_path` tests `_apply_code_source` directly; no test boots a real daemon through
  `supervisor.ensure_running()` from a runtime. The end-to-end hazard test exercises the broker
  respawn, not the supervisor. `nelix-3rm` will have a caller worth doing that against.
- **`PLUGIN_ROOT` still lies** (known debt, deliberately left): its value is right, its name says
  "plugin". A pure rename to `CORE_ROOT`, still the natural first step of the locator work.
- **No garbage collection.** Every install is 78MB and nothing ever removes an old generation.
  `installed()` lists them; retention is not in this slice's scope, but the disk cost is now real
  and someone should own it.
- `docs/hermes-plugin-references.md:79` and `docs/product-specification.md:197,219` still describe
  `pyte`/`ptyprocess` auto-installing venv-scoped. Stale before this slice, staler now.
