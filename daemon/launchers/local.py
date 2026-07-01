import paths
from daemon.broker_client import get_broker
from daemon.drivers import DRIVERS
from daemon.hook_settings import hook_launch
from daemon.launchers.base import ExecutorCapabilities
from daemon.pty_session import PtySession


def _driver_hook_capable(driver_name):
    # Read the class attribute off the registry (never instantiates). An unregistered/unknown
    # driver is treated as NOT hook-capable, so the launcher never crashes on a stray name and
    # only injects for a driver that opted in (ClaudeDriver.hook_capable = True).
    cls = DRIVERS.get(driver_name)
    return bool(getattr(cls, "hook_capable", False))


class LocalLauncher:
    """Run the executor as a host process in a PTY. Isolation == host. The actual fork+exec
    happens in the single-threaded broker (daemon.pty_broker), never in the daemon."""

    capabilities = ExecutorCapabilities(isolation_class="host", can_attach=False)

    def start(self, spec, cwd, cols=120, rows=40, dialog=None, transcript=None,
              *, session_id=None, hook_secret=None):
        argv = spec.argv()
        env = spec.resolved_env()
        # For a hook-capable driver with a session id + per-session secret, fold the additive
        # --settings hook config into argv and the NELIX_* addressing into env BEFORE the spawn.
        # Never touches the user's config; injection is skipped for hookless drivers (fallback path).
        if session_id and hook_secret and _driver_hook_capable(spec.driver):
            inj = hook_launch(session_id, str(paths.rpc_sock()), hook_secret)
            argv = [*argv, *inj["argv_extra"]]
            env = {**env, **inj["env"]}
        master_fd, pid, pgid = get_broker().spawn(argv, cwd, env, cols, rows)
        return PtySession(master_fd, pid, pgid, cols=cols, rows=rows,
                          dialog=dialog, transcript=transcript)

    def stop(self, handle):
        handle.close()
