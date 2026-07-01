from daemon.hooks import HookEvent, HookObservation, normalize_claude_hook as N


def E(event, **kw):
    return HookEvent(session_id="s-1", event=event, **kw)


def test_stop_is_idle_and_closes_turn():
    o = N(E("Stop"))
    assert o.kind == "idle" and o.closes_turn and not o.opens_turn


def test_stopfailure_is_idle():
    assert N(E("StopFailure")).kind == "idle"


def test_stop_interrupt_flag():
    assert N(E("Stop", is_interrupt=True)).interrupted is True


def test_userpromptsubmit_opens_working_turn():
    o = N(E("UserPromptSubmit"))
    assert o.kind == "working" and o.opens_turn


def test_pretooluse_is_working():
    assert N(E("PreToolUse", tool_name="Bash")).kind == "working"


def test_posttooluse_is_working():
    assert N(E("PostToolUse", tool_name="Bash")).kind == "working"


def test_posttoolusefailure_is_working():
    assert N(E("PostToolUseFailure", tool_name="Bash")).kind == "working"


def test_permissionrequest_is_waiting_permission():
    o = N(E("PermissionRequest", tool_name="Bash"))
    assert o.kind == "waiting_for_user" and o.prompt_kind == "permission_choice"


def test_notification_permission_is_waiting():
    assert N(E("Notification", notification="permission_prompt")).kind == "waiting_for_user"


def test_notification_idle_is_idle():
    assert N(E("Notification", notification="idle_prompt")).kind == "idle"


def test_pretooluse_askuserquestion_is_waiting_modal():
    o = N(E("PreToolUse", tool_name="AskUserQuestion", tool_input={"question": "JSON or YAML?"}))
    assert o.kind == "waiting_for_user" and o.prompt_kind == "modal_choice"


def test_posttooluse_askuserquestion_clears_pending():
    o = N(E("PostToolUse", tool_name="AskUserQuestion"))
    assert o.clears_pending and o.kind == "working"
