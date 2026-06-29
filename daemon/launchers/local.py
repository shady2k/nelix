from daemon.broker_client import get_broker
from daemon.launchers.base import ExecutorCapabilities
from daemon.pty_session import PtySession


class LocalLauncher:
    """Run the executor as a host process in a PTY. Isolation == host. The actual fork+exec
    happens in the single-threaded broker (daemon.pty_broker), never in the daemon."""

    capabilities = ExecutorCapabilities(isolation_class="host", can_attach=False)

    def start(self, spec, cwd, cols=120, rows=40, dialog=None, transcript=None):
        master_fd, pid, pgid = get_broker().spawn(
            spec.argv(), cwd, spec.resolved_env(), cols, rows)
        return PtySession(master_fd, pid, pgid, cols=cols, rows=rows,
                          dialog=dialog, transcript=transcript)

    def stop(self, handle):
        handle.close()
