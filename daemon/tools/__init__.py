"""Executor-facing CLIs (`nelix-question`, `nelix-note`) as importable modules.

They live under `daemon/` rather than in `bin/` because a console script needs an importable
`module:function` target, and because `daemon/hook_settings.py` hands their paths to a worker: the
thing the daemon promises and the thing the wheel installs are now the same artifact. `bin/nelix-*`
are thin shims over these for the source checkout.
"""
