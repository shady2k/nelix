"""`nelix rpc` read verbs against a REAL router subprocess. These assert the CLI CONTRACT — one
JSON envelope on stdout, the documented exit classes — not router behavior, which router/'s own
suite owns."""
import json

import pytest

import nelix_cli
from nelix_cli import envelope

OWNER = "harness-x"


def test_status_with_no_router_is_unavailable_not_a_traceback(capsys):
    rc = nelix_cli.main(["rpc", "status", "--owner", OWNER])

    assert rc == envelope.EXIT_UNAVAILABLE
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
    assert body["error"]["code"] == "router_unavailable"


def test_status_against_a_real_router_returns_the_board_envelope(real_router, capsys):
    assert nelix_cli.main(["daemon", "ensure"]) == 0
    capsys.readouterr()                                    # drop ensure's own object

    rc = nelix_cli.main(["rpc", "status", "--owner", OWNER])

    assert rc == envelope.EXIT_OK
    body = json.loads(capsys.readouterr().out)
    assert body["cli_api"] == 1
    assert body["ok"] is True
    assert body["sessions"] == {}


def test_dialog_requires_a_session(capsys):
    with pytest.raises(SystemExit) as ei:
        nelix_cli.main(["rpc", "dialog", "--owner", OWNER])

    assert ei.value.code == envelope.EXIT_USAGE
    assert "--session" in capsys.readouterr().err


def test_an_unknown_session_is_rejected_not_unavailable(real_router, capsys):
    assert nelix_cli.main(["daemon", "ensure"]) == 0
    capsys.readouterr()

    rc = nelix_cli.main(["rpc", "screen", "--owner", OWNER, "--session", "s-" + "0" * 8])

    assert rc == envelope.EXIT_REJECTED
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False
