"""The doorbell: what the model actually sees when it is woken. Two properties are load-bearing —
it carries ONLY small triage fields (the wake rides a bounded, tail-truncating channel, so screen
content would push the actionable fields out), and it always ends with the exact re-arm command
carrying the ADVANCED cursor (a doorbell that does not teach its own re-arm is how the loop dies)."""
from nelix_cli import doorbell

OWNER = "harness-x"
ORCH = "o-" + "1" * 32

EVENT_BODY = {
    "event": {"session_id": "s-abcd1234", "seq": 7, "kind": "waiting_for_user",
              "requires_response": True, "hung": False,
              "screen_excerpt": "SECRET SCREEN TEXT" * 500},
    "cursor": "cursor-token-2",
}


def test_classify_names_an_event_and_keeps_the_cursor():
    out = doorbell.classify(EVENT_BODY)

    assert out["reason"] == "event"
    assert out["cursor"] == "cursor-token-2"
    assert out["events"][0]["session_id"] == "s-abcd1234"


def test_classify_drops_everything_that_is_not_a_triage_field():
    out = doorbell.classify(EVENT_BODY)

    assert set(out["events"][0]) == set(doorbell.DOORBELL_FIELDS)


def test_classify_marks_a_resync_reply():
    assert doorbell.classify({"event": None, "cursor_expired": True})["reason"] == "resync"
    assert doorbell.classify({"event": None, "board_changed": True})["reason"] == "resync"


def test_classify_marks_an_empty_window():
    assert doorbell.classify({"event": None})["reason"] == "none"


def test_classify_marks_an_orchestration_with_nothing_to_wait_on():
    """The router answers this INSTANTLY (no sessions to poll). It must be terminal, or the
    waiter's window loop would spin on it at full speed."""
    assert doorbell.classify({"event": None, "empty_orchestration": True})["reason"] == "empty"


def test_render_contains_no_screen_content():
    text = doorbell.render(doorbell.classify(EVENT_BODY), owner=OWNER, orchestration=ORCH)

    assert "SECRET SCREEN TEXT" not in text


def test_render_ends_with_the_rearm_command_carrying_the_next_cursor():
    text = doorbell.render(doorbell.classify(EVENT_BODY), owner=OWNER, orchestration=ORCH)

    assert "nelix rpc status --owner harness-x" in text
    assert (f"nelix wait --owner {OWNER} --orchestration {ORCH} --cursor cursor-token-2"
            in text.splitlines()[-1])


def test_a_silent_window_still_teaches_the_rearm():
    text = doorbell.render(doorbell.classify({"event": None, "cursor": "c-9"}),
                           owner=OWNER, orchestration=ORCH)

    assert "no events" in text
    assert "--cursor c-9" in text.splitlines()[-1]


import nelix_cli
from nelix_cli import wait_cmd


def test_wait_polls_another_window_when_one_closes_empty(monkeypatch, capsys):
    """The router's window is a fixed ~25s. A waiter that returned after ONE empty window would
    wake the model every 25 seconds forever — so it must keep polling, carrying the cursor."""
    replies = [{"event": None, "cursor": "c-1"},
               {"event": None, "cursor": "c-2"},
               {"event": {"session_id": "s-abcd1234", "seq": 3, "kind": "idle",
                          "requires_response": False, "hung": False}, "cursor": "c-3"}]
    seen_cursors = []

    def fake_poll(owner_id, orchestration_id, cursor):
        seen_cursors.append(cursor)
        return replies[len(seen_cursors) - 1]

    monkeypatch.setattr(wait_cmd, "_poll", fake_poll)
    monkeypatch.setattr(wait_cmd.daemon_cmds, "_router_health", lambda timeout=2: {"ok": True})
    monkeypatch.setattr(wait_cmd, "_MIN_INTERVAL", 0.0)

    rc = nelix_cli.main(["wait", "--owner", OWNER, "--orchestration", ORCH])

    assert rc == 0
    assert seen_cursors == [None, "c-1", "c-2"]        # carried across every window seam
    assert "s-abcd1234" in capsys.readouterr().out


def test_wait_gives_up_at_its_deadline_and_still_teaches_the_rearm(monkeypatch, capsys):
    monkeypatch.setattr(wait_cmd, "_poll",
                        lambda owner, orch, cursor: {"event": None, "cursor": "c-9"})
    monkeypatch.setattr(wait_cmd.daemon_cmds, "_router_health", lambda timeout=2: {"ok": True})

    rc = nelix_cli.main(["wait", "--owner", OWNER, "--orchestration", ORCH, "--timeout", "0"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "no events" in out
    assert out.strip().splitlines()[-1].endswith("--cursor c-9")


def test_wait_does_not_spin_on_an_empty_orchestration(monkeypatch, capsys):
    """The router answers empty_orchestration INSTANTLY — one call, then out."""
    calls = []

    def fake_poll(owner_id, orchestration_id, cursor):
        calls.append(cursor)
        return {"event": None, "empty_orchestration": True}

    monkeypatch.setattr(wait_cmd, "_poll", fake_poll)
    monkeypatch.setattr(wait_cmd.daemon_cmds, "_router_health", lambda timeout=2: {"ok": True})

    rc = nelix_cli.main(["wait", "--owner", OWNER, "--orchestration", ORCH])

    assert rc == 0
    assert len(calls) == 1
    assert "no sessions to wait on" in capsys.readouterr().out
