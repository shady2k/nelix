"""A minimal TOML writer for ONE executor table.

`tomllib` reads TOML but cannot write it, and the alternative — a third-party writer — is a runtime
dependency this package is not allowed to have. The scope here is deliberately tiny: strings, string
arrays, and string tables, which is the whole shape of an executor entry. Anything richer belongs in
a hand-edited file, not in a generated one.
"""


def _string(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return f'"{escaped}"'


def _array(values) -> str:
    return "[" + ", ".join(_string(v) for v in values) + "]"


def _table(mapping) -> str:
    return "{ " + ", ".join(f"{k} = {_string(v)}" for k, v in mapping.items()) + " }"


def executor_table(name: str, spec: dict) -> str:
    """The `[executors.<name>]` block for `spec`, ending with a trailing newline. Keys are written
    in a fixed order so a regenerated file diffs cleanly."""
    lines = [f"[executors.{name}]"]
    for key in ("command", "driver", "launcher"):
        if spec.get(key) is not None:
            lines.append(f"{key} = {_string(spec[key])}")
    if spec.get("args"):
        lines.append(f"args = {_array(spec['args'])}")
    if spec.get("env"):
        lines.append(f"env = {_table(spec['env'])}")
    return "\n".join(lines) + "\n"
