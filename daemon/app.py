import os

from daemon.config import load_executors
from daemon.drivers import get_driver
from daemon.rpc_server import make_server
from daemon.session import Session


def _pick_executor(execs):
    name = os.environ.get("NELIX_EXECUTOR")
    if name:
        return name
    if len(execs) == 1:
        return next(iter(execs))
    raise SystemExit(f"set NELIX_EXECUTOR (configured: {sorted(execs)})")


def main():
    execs = load_executors(os.environ.get("NELIX_CONFIG", "nelix.toml"))
    spec = execs[_pick_executor(execs)]
    cwd = spec.resolved_cwd()
    os.makedirs(cwd, exist_ok=True)
    session = Session(get_driver(spec.driver), argv=spec.argv(),
                      env=spec.resolved_env(), cwd=cwd)
    token = os.environ["NELIX_RPC_TOKEN"]
    port = int(os.environ.get("NELIX_RPC_PORT", "8765"))
    server = make_server(session, token=token, port=port)
    print(f"nelix daemon: http://127.0.0.1:{port}  cwd={cwd}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        session.stop()


if __name__ == "__main__":
    main()
