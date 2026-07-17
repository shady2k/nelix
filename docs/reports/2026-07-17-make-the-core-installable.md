# Make the core installable [nelix-9a4.1] — worker report

**Branch:** `shady2k/nelix_installable` (2 commits on `4d83167`). **Not pushed.**

**Result:** the core builds as a wheel, installs into a clean venv, and runs from it — imports
`daemon.app`, renders through the packaged WASM, spawns and drives a real PTY, and hands a worker
hook paths that exist and execute.

| measurement | number |
|---|---|
| `make test` | **1298 passed, 3 skipped** |
| baseline (re-measured at base, this work stashed) | **1288 passed, 3 skipped** — confirmed |
| delta | +10 (7 `test_real_wheel.py`, 3 `test_hook_settings.py`) |
| mutations applied / caught | **7 / 7** |

The one `PytestUnhandledThreadExceptionWarning` (`pty_session` → `ghostty.feed` after `close()`
nulls `_mem`) **fires identically at base** with my work stashed. Pre-existing, in code I did not
touch. Not fixed here.

## The plan was evidence, not scripture: what measured false

You predicted a seventh disproof. There were three, plus an architecture correction.

**1. `ptyprocess` is not a runtime dep.** The plan lists it as `← runtime`. An AST walk over every
import under `daemon/` finds exactly **one** third-party module: `wasmtime`, in
`renderer/ghostty.py`. The PTY is stdlib — `os.openpty` + `os.login_tty`
(`pty_broker.py:109,57`). The string `ptyprocess` does not appear anywhere under `daemon/`.
`requirements.txt` and `supervisor.py:71` (`_DAEMON_MODULES`, commented "top-level imports") both
assert it; both are stale. I left `supervisor.py` alone — it is `nelix-3rm`'s — but the install does
not repeat the claim. Not just grepped: **the installed core spawns and drives a real PTY in a venv
where `ptyprocess` is not installed** (`test_installed_core_spawns_and_drives_a_real_pty`).

**2. `pyyaml` is not a runtime dep** (you flagged this to measure). Zero references under `daemon/`.
Only `bin/nelix-doctor` imports it, and `nelix-doctor` does not ship. It moves to the dev extra.

**3. The console-script mechanism you left open is a trap in the obvious form.** The plan suggests
`Path(sys.executable).parent`. `Path(sys.executable).resolve().parent` — the natural way to write
it — **walks out of the venv**: a uv-created venv symlinks `bin/python` at the uv-managed base
CPython, so it resolves to `~/.local/share/uv/python/cpython-3.11.15-*/bin`. This is not exotic;
**this repo's own `.venv` does it**. I found this only because the wheel test went red on it. The
answer is `sysconfig.get_path("scripts")`, which derives from `sys.prefix` — a venv sets it, and a
symlink cannot confuse it. `_tool_path()` probes the installed script dir, falls back to the
checkout's `bin/`, and **raises** if neither is executable.

**4. The architecture was wrong in both directions.** The plan says the pyproject packages
"`daemon/` (+ its `shim.wasm`) and the two existing `packages/`".

- *Too narrow:* `daemon/` imports the **top-level modules** `paths.py` (9 modules, including
  `app.py`) and `launcher_resolve.py` (`launchers/__init__.py`). Package `daemon/` alone and the
  wheel installs fine and dies at `import daemon.app`. They ship as `py-modules` (mutation 4).
- *Too broad:* `nelix_store` / `nelix_contracts` have **zero** references from `daemon/`, `bin/` or
  the top-level modules — test-only, matching "still zero refs from `daemon/`". Depending on them
  would make the wheel **uninstallable from a clean venv**: neither is published, so
  `uv pip install <wheel>` would go to PyPI and fail. They stay the separate dists they already are.

## The test was reading its own residue

Mutation 1 (drop `shim.wasm` from package data) came back **green**. The guard was a no-op — and the
fault was in my test, not the packaging.

setuptools' `build_py` copies sources into `build/lib/` and **never prunes stale entries**. Once the
repo had been built once with `shim.wasm` declared, every later `uv build` re-shipped it from the
leftovers regardless of what `pyproject.toml` said. I confirmed it directly: with
`include-package-data = false` *and* no `package-data` at all, `shim.wasm` was still in the wheel —
because `build/lib/daemon/renderer/shim.wasm` was sitting there from the control build.

The fixture now `copytree`s the working tree (minus `.git`, `.venv`, `build`, `dist`, `*.egg-info`,
caches, `spikes`) into `tmp_path` and builds there — a fresh clone is the real user. It also stops
the test littering the repo with `build/` + `*.egg-info`, which the first version did.

This is the whole argument for the mutation bar in one incident: the guard looked fine, the suite
was green, and it was measuring nothing.

## Detection power: 7 mutations, each alone, each red (control: 15 passed)

| # | mutation | red |
|---|---|---|
| 1 | drop `shim.wasm` package-data | 1 — `renders_through_packaged_wasm` |
| 2 | revert `hook_settings` to the repo-layout derivation | 4 — 2 wheel, 2 source |
| 3 | `pytest` back into runtime deps | 1 — `does_not_drag_in_pytest` |
| 4 | empty `py-modules` | 2 — `imports_the_daemon`, leak guard |
| 5 | drop the `+x` check | 1 — `ignores_a_non_executable_file` |
| 6 | entry point → nonexistent function | 1 — `hook_executables_actually_run` |
| 7 | `ptyprocess` back into runtime deps | 1 — `spawns_and_drives_a_real_pty` |

No guard here is a diagnostic. **Mutation 6 is why "the path exists" and "the path runs" are
separate tests**: pip still created the script, so it existed and was `+x` — the path-existence
assertion stayed green, and only the run assertion caught it. That is the same silent-success shape
as the bug this slice fixes.

## Decisions you asked me to make and justify

**Build backend: setuptools.** Both `packages/*/pyproject.toml` already use
`setuptools>=68` + `setuptools.build_meta`. One build system in one repo; nothing here needs
anything hatchling does better.

**Only the two worker-facing CLIs ship as console scripts.** `hook_settings.py` names
`nelix-question` / `nelix-note` by absolute path to every hook-capable executor, so an install
without them is a broken core — they are the daemon's own runtime requirement, not a convenience.
`nelix-doctor`/`-reap`/`-wait`/`-capture`/`-inventory` **do not ship**: they would drag in
`supervisor.py` + `rpc_client.py` (all three of doctor/reap/wait import them) and put `pyyaml` in
every install, in order to freeze a CLI surface `nelix-3rm` is about to replace with the `nelix`
CLI. You are right that an installed core you cannot inspect is a worse product; I judged one slice
of that cheaper than shipping names we plan to break. They keep working from the checkout, which is
where an operator has them today. **Reverse this if you disagree — it is one `[project.scripts]`
block.**

**`bin/nelix-question|note` became thin shims** over `daemon/tools/{question,note}.py` (logic moved
verbatim; the console script needs an importable `module:function`). The checkout keeps a working
executable, and `tests/test_nelix_wrappers.py` still exercises a real CLI.

**`requirements.txt` is now the dev/test env only** and uses `-e .`, so it no longer restates the
runtime deps — the drift the plan complained about cannot recur. Verified `make install` works in a
throwaway venv: both console scripts land in `bin/`, `wasmtime==45.0.0` arrives from the pyproject,
and `_tool_path` resolves to the *installed* script — so both resolution branches are exercised for
real, not just by monkeypatch.

## Loose ends I did not touch

- `supervisor.py:70-71`'s `_DAEMON_DEPS`/`_DAEMON_MODULES` still name `ptyprocess` as a "top-level
  import". False, and now provably so — but `supervisor.py` is out of scope by the plan's own
  instruction. `requirements-daemon.in/.lock` still pin it too.
- `daemon/session.py:563` names `nelix.toml.example` in an error message. Installed, that points at
  a file the user does not have. It is a docs pointer, not a data read (checked: nothing under
  `daemon/` opens it), so the wheel is complete — but the message is now slightly false off a
  checkout.
- `docs/hermes-plugin-references.md:79` still says `pyte`/`ptyprocess` auto-install.
