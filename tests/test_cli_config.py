"""`nelix config`: the wizard's only door into $NELIX_HOME/nelix.toml. The contract is that
whatever this writes, the DAEMON's own loader can read back — so every add is proved by a
round-trip through daemon.config.load_executors, not by string comparison."""
import json


import nelix_cli
import paths
from daemon.config import load_executors
from nelix_cli import envelope, toml_emit


def test_emitter_quotes_and_escapes_strings():
    text = toml_emit.executor_table("weird", {"command": 'a"b\\c', "driver": "claude"})

    assert '[executors.weird]' in text
    assert 'command = "a\\"b\\\\c"' in text


def test_emitter_writes_string_arrays():
    text = toml_emit.executor_table("x", {"command": "claude", "driver": "claude",
                                          "args": ["--interactive", "-v"]})

    assert 'args = ["--interactive", "-v"]' in text


def test_add_writes_an_executor_the_daemon_loader_accepts(capsys):
    rc = nelix_cli.main(["config", "add", "--name", "coder", "--command", "sh",
                         "--arg=--interactive"])

    assert rc == envelope.EXIT_OK
    loaded = load_executors(paths.config_path())
    assert loaded.parse_error is None
    assert loaded.specs["coder"].command == "sh"
    assert loaded.specs["coder"].args == ["--interactive"]
    assert loaded.specs["coder"].driver == "claude"


def test_add_is_refused_when_the_name_already_exists(capsys):
    assert nelix_cli.main(["config", "add", "--name", "coder", "--command", "sh"]) == 0
    capsys.readouterr()

    rc = nelix_cli.main(["config", "add", "--name", "coder", "--command", "sh"])

    assert rc == envelope.EXIT_REJECTED
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "executor_exists"


def test_add_is_refused_when_the_command_is_not_on_path(capsys):
    rc = nelix_cli.main(["config", "add", "--name", "ghost",
                         "--command", "definitely-not-installed-xyz"])

    assert rc == envelope.EXIT_REJECTED
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "command_not_found"


def test_add_appends_without_destroying_an_existing_executor(capsys):
    assert nelix_cli.main(["config", "add", "--name", "one", "--command", "sh"]) == 0
    assert nelix_cli.main(["config", "add", "--name", "two", "--command", "sh"]) == 0
    capsys.readouterr()

    loaded = load_executors(paths.config_path())
    assert sorted(loaded.specs) == ["one", "two"]


def test_list_reports_the_configured_executors(capsys):
    assert nelix_cli.main(["config", "add", "--name", "coder", "--command", "sh"]) == 0
    capsys.readouterr()

    assert nelix_cli.main(["config", "list"]) == envelope.EXIT_OK
    body = json.loads(capsys.readouterr().out)
    assert body["executors"]["coder"]["command"] == "sh"


def test_validate_reports_a_missing_config_without_crashing(capsys):
    rc = nelix_cli.main(["config", "validate"])

    assert rc == envelope.EXIT_REJECTED
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "config_unreadable"
