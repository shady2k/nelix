"""`nelix launcher install` — the verb the bootstrapper will call, and the one an operator uses to
repair a launcher. It answers through the same cli_api envelope as every other verb."""
import json
import os

import nelix_cli
import launcher
from nelix_cli import envelope


def test_install_writes_the_launcher_and_reports_where(tmp_path, capsys):
    rc = nelix_cli.main(["launcher", "install", "--home", str(tmp_path)])

    assert rc == envelope.EXIT_OK
    body = json.loads(capsys.readouterr().out)
    assert body["cli_api"] == 1
    assert body["ok"] is True
    assert body["path"] == str(tmp_path / "bin" / "nelix")
    assert os.access(tmp_path / "bin" / "nelix", os.X_OK)


def test_show_reports_an_absent_launcher_without_pretending(tmp_path, capsys):
    rc = nelix_cli.main(["launcher", "show", "--home", str(tmp_path)])

    assert rc == envelope.EXIT_REJECTED
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
    assert body["error"]["code"] == "launcher_absent"


def test_show_reports_a_stale_launcher_as_stale(tmp_path, capsys):
    path = launcher.install(tmp_path)
    path.write_text("stale\n")

    assert nelix_cli.main(["launcher", "show", "--home", str(tmp_path)]) == envelope.EXIT_OK
    body = json.loads(capsys.readouterr().out)
    assert body["current"] is False, "a launcher whose content differs must not be reported current"


def test_install_refreshes_a_stale_launcher(tmp_path, capsys):
    path = launcher.install(tmp_path)
    path.write_text("stale\n")
    capsys.readouterr()

    assert nelix_cli.main(["launcher", "install", "--home", str(tmp_path)]) == envelope.EXIT_OK

    assert path.read_text() == launcher.DISPATCHER
