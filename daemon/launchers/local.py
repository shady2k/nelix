from daemon.launchers.base import ExecutorCapabilities
from daemon.pty_session import PtySession


class LocalLauncher:
    """Run the executor as a host process in a PTY. Isolation == host."""

    capabilities = ExecutorCapabilities(isolation_class="host", can_attach=False)

    def start(self, spec, cols=120, rows=40):
        pty = PtySession(spec.argv(), cwd=spec.resolved_cwd(), cols=cols, rows=rows,
                         env=spec.resolved_env())
        pty.spawn()
        return pty

    def stop(self, handle):
        handle.close()
