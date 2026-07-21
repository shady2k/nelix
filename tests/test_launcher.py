"""The stable launcher is the ONLY path an adapter knows into the core, so its failure modes matter
more than its happy path: it runs on a system python that cannot import anything of ours, and it
must be honest when the core is not installed rather than crashing or silently picking some other
build.

These tests drive the REAL script through a real interpreter, with a stub runtime standing in for an
installed one — the dispatcher's whole job is exec + environment, and neither survives being faked.
"""
import json
import os
import subprocess
import sys
import launcher


def _make_home(tmp_path, build="b-1111"):
    """A NELIX_HOME with one installed-looking runtime whose `nelix` reports how it was invoked."""
    home = tmp_path / "home"
    rt = home / "runtimes" / build / "venv" / "bin"
    rt.mkdir(parents=True)
    stub = rt / "nelix"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'pinned': os.environ.get('NELIX_PINNED_BUILD'),\n"
        "                  'exe': sys.argv[0]}))\n")
    stub.chmod(0o755)
    (home / "runtimes" / "current").symlink_to(build)
    return home


def _run(path, args, home, extra_env=None):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "NELIX_HOME": str(home)}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(path), *args],
                          capture_output=True, text=True, env=env)


def test_install_writes_an_executable_launcher_atomically(tmp_path):
    home = tmp_path / "home"
    path = launcher.install(home)

    assert path == home / "bin" / "nelix"
    assert path.exists()
    assert os.access(path, os.X_OK), "the launcher must be executable"
    assert not list((home / "bin").glob("*.tmp*")), "no temp file may survive the install"


def test_install_is_idempotent_and_replaces_in_place(tmp_path):
    home = tmp_path / "home"
    first = launcher.install(home)
    first.write_text("stale\n")

    second = launcher.install(home)

    assert second == first
    assert second.read_text() == launcher.DISPATCHER, "a re-install must refresh a stale launcher"


def test_it_execs_the_runtime_binary_and_passes_argv_through(tmp_path):
    home = _make_home(tmp_path)
    path = launcher.install(home)

    r = _run(path, ["rpc", "status", "--owner", "harness-x"], home)

    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["argv"] == ["rpc", "status", "--owner", "harness-x"]
    assert got["exe"].endswith("runtimes/b-1111/venv/bin/nelix")


def test_it_pins_the_build_it_dispatched_into(tmp_path):
    home = _make_home(tmp_path)
    path = launcher.install(home)

    got = json.loads(_run(path, ["--version"], home).stdout)

    assert got["pinned"] == "b-1111", (
        "the dispatched process must be told which build it is, so nothing downstream re-reads "
        "`current` and gets a different answer")


def test_an_absent_current_is_an_actionable_error_not_a_traceback(tmp_path):
    home = tmp_path / "home"
    (home / "runtimes").mkdir(parents=True)
    path = launcher.install(home)

    r = _run(path, ["rpc", "status"], home)

    assert r.returncode == 3, "an absent core is the cli_api unavailable class, like the CLI's own"
    assert "Traceback" not in r.stderr
    assert "not installed" in r.stderr.lower()
    assert str(home) in r.stderr, "the message must name the NELIX_HOME it looked in"


def test_a_dangling_current_names_the_missing_build(tmp_path):
    home = _make_home(tmp_path)
    (home / "runtimes" / "current").unlink()
    (home / "runtimes" / "current").symlink_to("b-gone")
    path = launcher.install(home)

    r = _run(path, ["rpc", "status"], home)

    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "b-gone" in r.stderr


def test_a_runtime_without_its_nelix_binary_is_named_too(tmp_path):
    home = _make_home(tmp_path)
    (home / "runtimes" / "b-1111" / "venv" / "bin" / "nelix").unlink()
    path = launcher.install(home)

    r = _run(path, ["rpc", "status"], home)

    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "b-1111" in r.stderr


def test_it_honours_an_explicit_nelix_home_over_the_default(tmp_path):
    home = _make_home(tmp_path)
    path = launcher.install(home)

    got = json.loads(_run(path, ["x"], home, extra_env={"HOME": str(tmp_path / "elsewhere")}).stdout)

    assert got["pinned"] == "b-1111", "NELIX_HOME must win over ~/.nelix"


def test_the_dispatcher_imports_nothing_of_ours(tmp_path):
    """It runs on a system python that has never seen the core. One `import paths` would make it
    fail precisely on the machine whose broken install it exists to diagnose."""
    import ast

    tree = ast.parse(launcher.DISPATCHER)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])

    assert roots <= {"os", "sys"}, f"the dispatcher may only use os/sys, got {sorted(roots)}"
