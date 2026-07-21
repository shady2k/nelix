"""`nelix rpc` write verbs. The load-bearing assertion is the free-text path: a multi-line,
Cyrillic, quote-bearing brief must reach the router BYTE-FOR-BYTE, because removing that escaping
layer is the whole reason the contract takes files/stdin instead of a JSON argument."""
import io
import json

import nelix_cli
from nelix_cli import rpc
from nelix_cli import envelope

OWNER = "harness-x"

TASK = 'Почини логин:\n  - добавь тесты\n  - не трогай "билд"\nи всё\n'


def test_read_text_from_a_file_is_byte_exact(tmp_path):
    f = tmp_path / "task.txt"
    f.write_text(TASK, encoding="utf-8")

    assert rpc.read_text(str(f)) == TASK


def test_read_text_from_stdin_is_byte_exact(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(TASK))

    assert rpc.read_text("-") == TASK


def test_start_sends_the_task_unmodified(monkeypatch, tmp_path, capsys):
    sent = {}

    def fake_post(owner_id, path, payload):
        sent.update(path=path, **payload)
        return {"operation": "start", "status": "started", "session_id": "s-abcd1234"}

    monkeypatch.setattr(rpc, "_post", fake_post)
    monkeypatch.setattr(rpc.daemon_cmds, "_router_health", lambda timeout=2: {"ok": True})
    f = tmp_path / "task.txt"
    f.write_text(TASK, encoding="utf-8")

    rc = nelix_cli.main(["rpc", "start", "--owner", OWNER, "--executor", "claude",
                         "--cwd", str(tmp_path), "--task-file", str(f)])

    assert rc == envelope.EXIT_OK
    assert sent["path"] == "/start"
    assert sent["task"] == TASK
    assert sent["idempotency_key"]                       # always sent: the route requires it
    body = json.loads(capsys.readouterr().out)
    assert body["session_id"] == "s-abcd1234"


def test_start_mints_an_orchestration_id_and_echoes_it(monkeypatch, tmp_path, capsys):
    """The reply from /start does not carry the orchestration id, and a caller that cannot name it
    cannot arm a waiter — so the CLI supplies it and reports what it supplied."""
    sent = {}
    monkeypatch.setattr(rpc, "_post",
                        lambda owner_id, path, payload: sent.update(payload) or {"status": "started"})
    monkeypatch.setattr(rpc.daemon_cmds, "_router_health", lambda timeout=2: {"ok": True})
    f = tmp_path / "task.txt"
    f.write_text("t", encoding="utf-8")

    assert nelix_cli.main(["rpc", "start", "--owner", OWNER, "--executor", "claude",
                           "--cwd", str(tmp_path), "--task-file", str(f)]) == 0

    body = json.loads(capsys.readouterr().out)
    assert body["orchestration_id"].startswith("o-")
    assert len(body["orchestration_id"]) == 34
    assert sent["orchestration_id"] == body["orchestration_id"]


def test_start_uses_the_orchestration_the_caller_named(monkeypatch, tmp_path, capsys):
    """N workers in ONE orchestration is what lets a single waiter cover all of them."""
    sent = {}
    monkeypatch.setattr(rpc, "_post",
                        lambda owner_id, path, payload: sent.update(payload) or {"status": "started"})
    monkeypatch.setattr(rpc.daemon_cmds, "_router_health", lambda timeout=2: {"ok": True})
    f = tmp_path / "task.txt"
    f.write_text("t", encoding="utf-8")
    orch = "o-" + "3" * 32

    assert nelix_cli.main(["rpc", "start", "--owner", OWNER, "--executor", "claude",
                           "--cwd", str(tmp_path), "--task-file", str(f),
                           "--orchestration", orch]) == 0

    assert sent["orchestration_id"] == orch
    assert json.loads(capsys.readouterr().out)["orchestration_id"] == orch


def test_restart_sends_only_what_the_router_route_accepts(monkeypatch, capsys):
    sent = {}
    monkeypatch.setattr(rpc, "_post",
                        lambda owner_id, path, payload: sent.update(path=path, **payload) or {"ok": 1})
    monkeypatch.setattr(rpc.daemon_cmds, "_router_health", lambda timeout=2: {"ok": True})

    assert nelix_cli.main(["rpc", "restart", "--owner", OWNER,
                           "--session", "s-abcd1234", "--force"]) == 0

    assert sent["path"] == "/restart"
    assert "new_session_id" not in sent               # the router assigns it
    assert sent["force"] is True


def test_respond_passes_the_decision_id_through(monkeypatch, tmp_path, capsys):
    sent = {}

    class FakeClient:
        def respond(self, session_id, answer, decision_id=None):
            sent.update(session_id=session_id, answer=answer, decision_id=decision_id)
            return True, {"status": "delivered"}

    monkeypatch.setattr(rpc, "client_for", lambda owner: FakeClient())
    monkeypatch.setattr(rpc.daemon_cmds, "_router_health", lambda timeout=2: {"ok": True})
    f = tmp_path / "answer.txt"
    f.write_text("да, ставь\n", encoding="utf-8")

    rc = nelix_cli.main(["rpc", "respond", "--owner", OWNER, "--session", "s-abcd1234",
                         "--answer-file", str(f), "--decision-id", "d-1"])

    assert rc == envelope.EXIT_OK
    assert sent == {"session_id": "s-abcd1234", "answer": "да, ставь\n", "decision_id": "d-1"}


def test_a_refused_respond_is_the_rejected_exit_class(monkeypatch, tmp_path, capsys):
    class FakeClient:
        def respond(self, session_id, answer, decision_id=None):
            return False, {"error": {"code": "missing_decision_id", "message": "name it"}}

    monkeypatch.setattr(rpc, "client_for", lambda owner: FakeClient())
    monkeypatch.setattr(rpc.daemon_cmds, "_router_health", lambda timeout=2: {"ok": True})
    f = tmp_path / "answer.txt"
    f.write_text("да", encoding="utf-8")

    rc = nelix_cli.main(["rpc", "respond", "--owner", OWNER, "--session", "s-abcd1234",
                         "--answer-file", str(f)])

    assert rc == envelope.EXIT_REJECTED
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "missing_decision_id"


def test_a_missing_task_file_is_a_usage_error(capsys):
    rc = nelix_cli.main(["rpc", "start", "--owner", OWNER, "--executor", "claude",
                         "--cwd", "/tmp", "--task-file", "/nope/absent.txt"])

    assert rc == envelope.EXIT_USAGE
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "unreadable_input"
