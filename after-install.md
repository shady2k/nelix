# Nelix installed

Nelix orchestrates an agentic CLI over a PTY and relays its decision points to you.

**One-time setup:** declare your executors in `$HERMES_HOME/workspace/nelix/nelix.toml`
(a starter copy was seeded on first load). Each entry maps a name to a command
nelix runs verbatim — secret injection or wrappers are entirely yours.

Then just ask in conversation, e.g. *"code with example_cli: refactor the auth module"*.
Nelix spawns its daemon, launches the CLI, and wakes the agent only at decision points.
The daemon is ephemeral (one per gateway) and is torn down on session end.

**Dependencies:** the daemon needs `pyte` and `ptyprocess`. On first use nelix installs
them into the Hermes runtime venv automatically (venv-scoped, honoring
`security.allow_lazy_installs`). If you disable lazy installs, install them yourself into
that venv: `<hermes-venv>/bin/pip install pyte==0.8.2 ptyprocess==0.7.0`.
