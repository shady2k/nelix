# NELIX_HOME: the core's state leaves the Hermes tree [nelix-9a4.7]

Worker report. Branch `shady2k/nelix_home`, base `main` @ 9b1a14e.

## What shipped

`paths.py` no longer knows what Hermes is. `nelix_root()` is the canonical `$NELIX_HOME`,
defaulting to `~/.nelix`; `hermes_home()` is deleted from `paths.py` and now lives in
`bin/nelix-doctor`, its only remaining consumer, where it belongs — `hermes_wiring` is a probe
*of* Hermes, not a place the core stores anything.

No migration was written. The greenfield claim checked out (`~/.nelix` absent, no daemon
running) — with one correction, below.

## Four claims in the brief that measurement disproved

### 1. "Baseline: 1298 passed, 3 skipped" — false. `main` was RED.

Measured on a clean tree at 9b1a14e: **1297 passed, 1 failed, 3 skipped**.
`tests/test_launch_injection.py::test_injects_executor_message_instructions_for_claude`,
deterministic, reproduces in isolation in 0.02s.

9a4.1 rewrote `hook_settings._tool_path` to prefer the installed console script (sysconfig
scripts dir) over `<repo>/bin`. The test still hardcoded `<_REPO_ROOT>/bin/nelix-question` and
did not monkeypatch, so it resolved against the real venv. Its result was a function of **venv
state, not code** — proven by moving the console scripts out of `.venv/bin` and back:

| `.venv/bin/nelix-question` | result |
|---|---|
| absent | 1 passed |
| present (what `requirements.txt`'s `-e .` creates) | 1 failed |

`_tool_path`'s own docstring contains both halves of the contradiction: candidate #1 says
"`-e .` (requirements.txt) ... installs the same console scripts", candidate #2 is justified as
"The suite runs this way" — a venv *without* the core installed. Both cannot be true.

Fixed here in a separate commit. The test now asserts the contract `_tool_path` was rewritten to
enforce — the path handed to a worker **exists and is executable** — instead of a hardcoded
location. Verified green in *both* venv states, which is the property the original lacked.

### 2. "Session raw PTY dumps and the state file are exposed to untrusted containers TODAY" — false.

The premise is that a root container bind-mounts `~/.hermes/workspace`, under which `nelix_root`
sits. Measured against the operator's actual config, no profile both runs containers and mounts a
path containing `nelix_root`:

| profile | `terminal.backend` | `docker_volumes` host path | contains `workspace/nelix`? | nelix enabled? |
|---|---|---|---|---|
| root (`~/.hermes/config.yaml`) | **docker** | `~/.hermes/workspace/**project**` | **no** — sibling of `nelix` | no |
| `local` | **local** | `~/.hermes/workspace` | **yes** | **yes** |

The profile with the dangerous whole-`workspace` mount (`local`) does not run containers; the
profile that does (root) mounts one level deeper on a sibling path and does not enable nelix.
`docker_run_as_host_user: false` in both, so the root-bypasses-perms half is real — it just has
nothing of ours to bypass. And `launcher_resolve` fails closed on any non-`local` backend, so
nelix cannot start a session under the docker profile at all.

So the exposure is **latent, one config change away — not live**. It becomes real the moment
`local`'s backend flips to docker, which is a plausible edit and not a loud one.

**Which exposure the move removes, precisely:** `~/.nelix` is outside `~/.hermes` entirely, so no
Hermes *workspace* mount can reach it regardless of which backend a profile later enables — the
`local` profile's whole-`workspace` mount included. That is a structural fix, not a permissions
one. **What it does not remove:** a mount naming `~/.nelix` or `$HOME` directly (`-v $HOME:/host`
still reads everything). "Out of the blast radius of the plausible workspace mounts" — not
"immune to bind-mounts".

### 3. "wake.py passes --state-file, not --token-file" — right about the flag, wrong about the file.

`wake.py` **does not exist in this repo**; it left with the plugin (4d83167). Within the core the
claim resolves against `bin/nelix-wait`, whose parser has `--state-file` (required), `--after` and
`--session-id`, and **no `--token-file` at any spelling**. g7b's text is stale as the note says.

**Waiter/container subtlety, re-verified:** `launcher_resolve.resolve_launcher` raises
`NotImplementedError` for `configured="auto"` on a non-local backend and `PermissionError` for
`configured="local"` under a stronger backend without `allow_weaker_than_profile`. Only `local`
is implemented, so **the waiter always runs host-side, same uid**. Moving the state file to
`~/.nelix/.active.json` (0600, owner-only) therefore cannot break it. When a container launcher
lands, the state file must be made reachable inside the container — the move turns that into an
explicit design decision instead of an accident of `~/.hermes/workspace` happening to be mounted.
**g7b is safe to close on this slice.**

### 4. "Nothing outside this repo reads the old path" (flagged unverified) — not quite.

`~/.hermes/profiles/local/config.yaml` has `plugins.enabled: ['nelix']`, and
`~/.hermes/profiles/local/plugins/nelix/` is a **live, enabled, deployed checkout** (@ 1fc025a)
whose `paths.py` is **byte-identical** to ours. It is a standalone copy (own `daemon/`, own
`supervisor.py`) that does not import this repo, so it cannot break from this change.

The consequence is divergence, not breakage: after this slice the core uses `~/.nelix` while the
deployed plugin keeps its own state at `~/.hermes/workspace/nelix`, and **the singleton lock no
longer spans both** — they are different inodes, so both could hold "the" daemon lock at once.
Harmless while the plugin is the only thing actually running, and it resolves when the harness is
re-pointed at the core. Worth knowing before `make deploy`.

## A bug the slice exposed: the suite writes PTY dumps into the operator's home

`~/.nelix/sessions/s1/{raw,capture,transcript.jsonl,meta.json}` appeared during my first run.
**Pre-existing, not introduced** — the suite has always done this, writing to
`~/.hermes/workspace/nelix/sessions/s1` before the move. The Jun 29 session dir the brief cites as
"one old session dir" is test residue.

I nearly missed it, and the way I missed it is worth recording: I checked the *directory* mtimes,
saw June, and concluded the suite did not write there. Overwriting a file does not change its
directory's mtime. `find -newermt` shows the files being rewritten on every run. **Directory
mtimes are not a write log.**

Closed with an autouse `isolate_nelix_home` fixture in `tests/conftest.py`, because a default root
is a real directory and no individual test can be trusted to remember to override it. Residue
removed (`~/.nelix`); `~/.hermes/workspace/nelix` left alone — not mine to delete.

## Detection power: 14/14 mutations caught

Every guard was broken and the test confirmed red. `A guard whose deletion changes nothing is a
diagnostic, not a guard.`

| guard broken | detected by |
|---|---|
| canonicalise: drop `.resolve()` | `test_symlink_alias_resolves_to_the_same_root` |
| canonicalise: drop `.resolve()` (traversal spelling) | `test_root_is_canonical_not_merely_absolute` |
| default: point the root back inside the Hermes tree | `test_root_names_no_harness_home` |
| blank env: treat `NELIX_HOME='   '` as a real value | `test_blank_env_falls_back_to_the_default` |
| sun_path: off-by-one (`>=` → `>`) | `test_sun_path_overflow_boundary_is_the_byte_the_kernel_rejects` |
| sun_path: remove the bind-site guard | `test_unix_bind_refuses_an_over_long_socket_path_naming_the_cause` |
| 0700: remove the chmod walk | `test_ensure_private_dir_is_0700_down_to_nelix_root` |
| 0700: walk one level past the root | `test_ensure_private_dir_leaves_ancestors_above_the_root_alone` |
| 0600: state file created world-readable | `test_private_opener_creates_0600` |
| isolation fixture: delete it | `test_the_isolation_fixture_is_in_force` |
| inventory: generator names the live root | `test_generator_source_has_no_live_sessions_path` |
| supervisor: stop pinning `NELIX_HOME` into the daemon env | `test_daemon_env_pins_the_resolved_nelix_home_and_drops_hermes_home` |
| app.py: eager default resurfaces (`dict.get`) | `test_explicit_rpc_sock_does_not_derive_the_nelix_home_node` |
| doctor: report the config under a Hermes profile again | `test_collect_adds_hermes_wiring_and_keeps_old_keys` |

Three notes on how that number was earned, since a clean 14/14 is exactly what a rigged harness
also prints:

- **The 0700 test passed by luck at the new location, as the brief suspected.** pytest creates
  `tmp_path` 0700 already, and `nelix_root()` *is* `tmp_path` now — so asserting 0700 on the root
  was vacuous. The test loosens the root to 0755 first; only then can 0700 come from the code.
- **My first "walk past the root" mutation came back GREEN and the test was right.** The mutation
  removed `p == root or`, which is *redundant* — `root` is never in `root.parents`, so the loop
  already stops there. A no-op mutation cannot be detected. The replacement walks genuinely one
  level up (and only one: walking to `/` would chmod `/private` to 0700 and wreck the machine).
- **The harness poisoned the bytecode cache and I nearly shipped the wrong conclusion.** After
  the first 14/14 the suite came back with three failures — `test_private_opener_creates_0600`
  among them — against source that read `0o600`. They reproduced in isolation, so my first
  instinct (concurrent runs) was wrong. The loaded code object's constant was **420** (`0o644`):
  my own mutant, still executing from a `.pyc` whose source file had been restored. Python
  invalidates a `.pyc` by **(source mtime, source size)**; `0o600` → `0o644` does not change the
  size, and mutate-run-restore fits inside one 1-second mtime tick — so the stale mutant was
  considered fresh, indefinitely. `inspect.getsource` cannot see this: it reads the *file*, so it
  showed the correct source while the wrong bytecode ran. The harness now purges `__pycache__`
  either side of every mutation and runs with `PYTHONDONTWRITEBYTECODE=1`; the table above is the
  re-measured result. **This is the "test reading its own build residue" family again, self-
  inflicted** — worth knowing before anyone writes another mutation harness here.

## Judgement calls, and one I got wrong

**The socket/lock stay in `nelix_root`.** The spec's "socket/lock at a per-uid runtime location
keyed by hash(canonical NELIX_HOME)" is `nelix-3rm`'s *router* design, and there is no router
here. I argued against it on three grounds and **one of them was wrong**:

- *(c) "sun_path doesn't bite at 28 of 103 chars"* — **disproved by my own guard.** True of the
  default; false of the general case. pytest's `tmp_path` socket is **125 bytes** against a
  measured limit of 103 (binds at 103, `EADDRTOOLONG` at 104). Under a temp root the *old* layout
  was 103 — exactly at the edge; the new one is 87, so this slice buys 16 bytes but neither
  survives an arbitrary root. **The repo already proves the spec right**: `test_rpc_server.py`'s
  `unix_sock` fixture is the spec in miniature — `md5(tmp_path)[:8]` → `/tmp/nx<hash>.sock` —
  invented independently because sun_path forced it. It is why no test binds at `paths.rpc_sock()`:
  it cannot.
- *(a) `/tmp` is world-writable* — still stands, and is a **requirement** for the router, not an
  argument against it: a per-uid runtime dir needs create-0700 + verify-owner-and-mode
  (the ssh-agent/tmux pattern) or it is a downgrade from today's node inside a 0700 root.
- *(b) inode beats string* — mostly recovered by canonicalise-first, which is presumably why the
  spec says it. The lock's **inode** is what enforces one-daemon-per-root today; the filesystem
  canonicalises aliases for free. Canonicalising in `nelix_root()` is belt-and-braces now and
  becomes load-bearing the moment anything keys off the root's *name*, which is what the router
  will do.

**Recommendation: do the relocation in `nelix-3rm`, with the ownership check.** Not here — it is
security-sensitive `/tmp` handling on a slice titled "the core's state leaves the Hermes tree".

**The sun_path guard is at the bind site, not in `rpc_sock()`.** I put it in the accessor first;
the 125-byte `tmp_path` red-lit my own test and proved an accessor that throws breaks every caller
that only wants the string. `sun_path_overflow()` returns a reason; `_make_unix_server` raises —
before constructing the server, because `server_bind()` unlinks the existing node first, so a late
failure destroys a live daemon's socket on the way down.

**`HERMES_HOME` is no longer materialised into the daemon env.** Nothing under `daemon/` reads it
(measured: zero hits). Injecting a harness's home into the daemon and every executor it launches is
the coupling this slice removes. A `HERMES_HOME` the operator really has set still reaches children
through `os.environ`; we just stop inventing one. `NELIX_HOME` is passed **resolved**, so the daemon
cannot land on a different root than the supervisor that spawned it.

**`nelix_doctor`'s `nelix_toml_present` is gone from the per-profile verdict.** It read
`<profile>/workspace/nelix/nelix.toml` — hand-built from literals, importing nothing, invisible to
a grep for `nelix_root`. This is the "a dependency is not only an import" case, and it is why the
brief's "grep for the path STRINGS too" was the right instruction. The config is a property of
`$NELIX_HOME` now, so a per-profile answer would be false for every profile forever; it is reported
once as `nelix_home` in `collect()`.

**`ensure_private_dir` now chmods the root itself.** It used to stop below a shared `HERMES_HOME`;
the root *is* `$NELIX_HOME` now, so the dir we tighten is the one the operator named for us.
Ancestors above it are still never touched (tested). Consequence worth naming: `NELIX_HOME=$HOME`
would chmod `$HOME` to 0700. Point it at a directory that is nelix's.
