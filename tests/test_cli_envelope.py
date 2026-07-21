"""The cli_api v1 envelope: exactly one JSON object on stdout, diagnostics on stderr only,
stable exit classes. Every `nelix rpc`/`wait`/`config` verb answers through these two helpers,
so their contract is tested once, here, rather than re-asserted per verb."""
import json

from nelix_cli import envelope


def test_emit_ok_prints_one_json_object_with_cli_api_and_returns_zero(capsys):
    rc = envelope.emit_ok({"sessions": {}})

    assert rc == envelope.EXIT_OK
    captured = capsys.readouterr()
    assert captured.err == ""
    body = json.loads(captured.out)          # raises if more than one object was printed
    assert body == {"cli_api": 1, "ok": True, "sessions": {}}


def test_emit_error_prints_json_on_stdout_message_on_stderr_and_returns_its_class(capsys):
    rc = envelope.emit_error("router_unavailable", "no router is running",
                             exit_class=envelope.EXIT_UNAVAILABLE)

    assert rc == 3
    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert body["cli_api"] == 1
    assert body["ok"] is False
    assert body["error"] == {"code": "router_unavailable", "message": "no router is running"}
    assert "no router is running" in captured.err


def test_emit_error_carries_optional_details(capsys):
    envelope.emit_error("rejected", "bad executor", exit_class=envelope.EXIT_REJECTED,
                        details={"executor": "nope"})

    body = json.loads(capsys.readouterr().out)
    assert body["error"]["details"] == {"executor": "nope"}


def test_payload_cannot_overwrite_the_envelope_keys(capsys):
    envelope.emit_ok({"cli_api": 99, "ok": False})

    body = json.loads(capsys.readouterr().out)
    assert body["cli_api"] == 1
    assert body["ok"] is True
