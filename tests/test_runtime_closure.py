"""runtime.py is the RUNTIME BUILDER, and the distribution design has it running in two places: the
installed core, and a stdlib-only `nelix-bootstrap.pyz` that cannot import daemon/ or router/ at all
(they are not in it). So its import closure is a CONTRACT, not a preference — this test states it in
the only form that survives refactoring: walk the AST of each module and of everything it imports
from this repo, and fail on the first daemon/router edge.
"""
import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]

# Modules that must be carryable by a stdlib-only bootstrapper.
STDLIB_ONLY = ("runtime", "paths")
FORBIDDEN_ROOTS = ("daemon", "router", "nelix_cli")


def _imported_roots(module_name: str) -> set:
    """The top-level names `module_name` imports, from its AST — including inside functions, which
    is exactly where the daemon import used to hide."""
    path = REPO / f"{module_name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _local_closure(seed: str) -> set:
    """seed plus every first-party top-level MODULE it reaches, transitively."""
    seen, todo = set(), [seed]
    while todo:
        name = todo.pop()
        if name in seen or not (REPO / f"{name}.py").exists():
            continue
        seen.add(name)
        todo.extend(_imported_roots(name))
    return seen


def test_the_runtime_builder_never_imports_the_daemon_or_the_router():
    for seed in STDLIB_ONLY:
        for module in _local_closure(seed):
            offenders = _imported_roots(module) & set(FORBIDDEN_ROOTS)
            assert not offenders, (
                f"{module}.py imports {sorted(offenders)}; {seed} must stay carryable by a "
                f"stdlib-only bootstrapper that has no daemon/ or router/ in it")


def test_the_closure_check_sees_an_import_hidden_inside_a_function(tmp_path, monkeypatch):
    """A guard nobody can see fail is not a guard — and this one has to catch the exact shape the
    violation had: `from daemon import singleton` nested INSIDE a function body, where a
    module-header scan would miss it."""
    offender = tmp_path / "sneaky.py"
    offender.write_text("import os\n\n\ndef install():\n    from daemon import singleton\n")
    monkeypatch.setattr(__import__(__name__), "REPO", tmp_path, raising=False)

    import tests.test_runtime_closure as mod
    monkeypatch.setattr(mod, "REPO", tmp_path)

    assert "daemon" in mod._imported_roots("sneaky")
