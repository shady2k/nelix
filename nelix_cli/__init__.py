"""The `nelix` CLI package. `main` and `ensure_router` are re-exported because callers outside this
package (bin/nelix, the suite) name them directly."""
from nelix_cli.cli import main
from nelix_cli.daemon_cmds import ensure_router

__all__ = ["main", "ensure_router"]
