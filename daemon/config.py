import os
import tomllib
from dataclasses import dataclass


@dataclass
class ExecutorSpec:
    command: str
    args: list
    env: dict
    cwd: str
    driver: str

    def argv(self):
        return [self.command, *self.args]

    def resolved_env(self):
        merged = dict(os.environ)
        for k, v in self.env.items():
            merged[k] = os.path.expanduser(str(v))
        return merged

    def resolved_cwd(self):
        return os.path.expanduser(self.cwd)


def load_executors(path):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    out = {}
    for name, spec in data.get("executors", {}).items():
        out[name] = ExecutorSpec(
            command=spec["command"],
            args=list(spec.get("args", [])),
            env=dict(spec.get("env", {})),
            cwd=spec.get("cwd", "."),
            driver=spec.get("driver", "claude"),
        )
    return out
