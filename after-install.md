# Nelix installed

Nelix orchestrates a coding agent (a CLI tool) over a PTY and relays its decisions to you.

**One-time setup:** declare your executors in `$HERMES_HOME/workspace/nelix/nelix.toml`
(a starter copy was seeded on first load). Each entry maps a name to a command
nelix runs verbatim — secret injection or wrappers are entirely yours.

Then just ask in conversation, e.g. *"code with example_cli: refactor the auth module"*. The agent
runs in your current directory, or a project path you name. Nelix spawns its daemon, launches the
agent, and wakes Hermes only when a decision is needed. The daemon is ephemeral (one per gateway)
and is torn down on session end.

**Dependencies:** the daemon needs `wasmtime` and `ptyprocess` (the terminal screen is
rendered by libghostty-vt running in-process via wasmtime; a pinned `shim.wasm` ships with
the plugin). On first use nelix installs them into the Hermes runtime venv automatically
(venv-scoped, honoring `security.allow_lazy_installs`). If you disable lazy installs, install
them yourself into that venv: `<hermes-venv>/bin/pip install wasmtime==45.0.0 ptyprocess==0.7.0`.
