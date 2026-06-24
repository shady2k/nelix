# Hermes plugin development — canonical references

Authoritative sources for the Hermes plugin API and manifest schema. **Consult these
first** when working on packaging, `plugin.yaml`, the `register(ctx)` surface, hooks,
commands, skills, or dependency handling — they override assumptions in any plan/spec.

- **Build-a-plugin guide:**
  https://github.com/NousResearch/hermes-agent/blob/main/website/docs/guides/build-a-hermes-plugin.md
- **Example plugins:**
  https://github.com/NousResearch/hermes-example-plugins

## Local source of truth (this machine)
The installed Hermes (`v0.17.0`) source is the ground truth alongside the docs above:

- Plugin loader / manifest parser / `PluginContext`: `~/.hermes/hermes-agent/hermes_cli/plugins.py`
- Install / manifest-version validation: `~/.hermes/hermes-agent/hermes_cli/plugins_cmd.py`
- Reference bundled plugin (daemon lifecycle pattern): `~/.hermes/hermes-agent/plugins/google_meet/`
  (`__init__.py`, `process_manager.py`, `plugin.yaml`)

### Verified manifest fields the parser actually reads (`plugins.py` PluginManifest)
`name`, `version`, `description`, `author`, `requires_env`, `provides_tools`,
`provides_hooks`, `kind` (one of `standalone|backend|exclusive|platform|model-provider`).
`manifest_version: 1` is validated on install (`plugins_cmd.py`).
Keys like `hooks:`, `provides_commands:`, `pip_dependencies:` are **not** parsed into the
manifest dataclass — google_meet declares `hooks:`/registers them at runtime via
`ctx.register_hook(...)`; dependency presence is verified at runtime via a `check_fn`
passed to `register_tool`, not auto-pip-installed.

## Plugin Python dependencies — the canonical pattern (verified)
There is **no manifest field** that installs a plugin's Python deps. `pip_dependencies:`
in `plugin.yaml` is a no-op. The verified ways to get extra deps into the **Hermes runtime
venv** (the venv whose `python` is the plugin's `sys.executable` — here
`~/.hermes/hermes-agent/venv`, python 3.11, which does **not** ship `pyte`/`ptyprocess`):

1. **Self pip-install, venv-scoped** (what `google_meet` does — `plugins/google_meet/cli.py`):
   `subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *pins])`.
   Comment in source: *"pip deps — always safe, venv-scoped."* Targets `sys.executable`,
   never system python.
2. **`tools.lazy_deps.ensure("feature")`** — Hermes' own lazy installer, BUT it only accepts
   features in the hardcoded `LAZY_DEPS` allowlist (`tools/lazy_deps.py`). Not extensible by
   a third-party plugin, so unusable for `pyte`/`ptyprocess`. It does expose the security
   gate worth honoring: `security.allow_lazy_installs` (config) / `HERMES_DISABLE_LAZY_INSTALLS=1`.
3. **`check_fn` on `register_tool`** — gates tool *visibility* (returns bool: importable?),
   does not install. `google_meet` pairs it with an explicit `hermes meet setup` CLI command
   (justified by its ~300MB chromium download).
4. **Entry-point plugin** (`pyproject.toml` + `[project.entry-points."hermes_agent.plugins"]`)
   — pip resolves deps at install time. Heavier; diverges from the directory-plugin layout.

**nelix's choice:** `pyte`/`ptyprocess` are tiny pure-python — no heavy setup step needed.
Auto-ensure (pattern 1) on first `nelix_start`, inside `supervisor.ensure_running()`,
honoring the pattern-2 security gate, with a clear manual-`pip` fallback message. Keeps
zero-config install true.
