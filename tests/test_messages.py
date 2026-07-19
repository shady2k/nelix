from daemon.messages import parse_message_body, AsyncQuestion, ProgressNote


def test_question_ok():
    obj, err = parse_message_body("question",
        {"question": "a or b?", "continuation_plan": "keep going", "assumption": "a"})
    assert err is None
    assert isinstance(obj, AsyncQuestion) and obj.question == "a or b?" and obj.assumption == "a"


def test_question_missing_continuation_plan():
    obj, err = parse_message_body("question", {"question": "a or b?"})
    assert obj is None and err == (400, "continuation_plan required")


def test_note_ok():
    obj, err = parse_message_body("note", {"summary": "step 2 done"})
    assert err is None and isinstance(obj, ProgressNote) and obj.details is None


def test_unknown_kind():
    obj, err = parse_message_body("chatter", {"x": 1})
    assert obj is None and err[0] == 400


def test_oversize_truncates():
    from daemon.config import MAX_SUMMARY_LEN
    obj, err = parse_message_body("note", {"summary": "x" * (MAX_SUMMARY_LEN + 50)})
    assert err is None and len(obj.summary) == MAX_SUMMARY_LEN


def test_question_missing_question():
    obj, err = parse_message_body("question", {"continuation_plan": "keep going"})
    assert obj is None and err == (400, "question required")


def test_note_missing_summary():
    obj, err = parse_message_body("note", {})
    assert obj is None and err == (400, "summary required")


def test_question_optional_fields_default_none():
    obj, err = parse_message_body("question",
        {"question": "a or b?", "continuation_plan": "keep going"})
    assert err is None
    assert obj.assumption is None and obj.impact_if_wrong is None


def test_question_impact_if_wrong_passthrough():
    obj, err = parse_message_body("question", {
        "question": "a or b?",
        "continuation_plan": "keep going",
        "impact_if_wrong": "wasted work",
    })
    assert err is None and obj.impact_if_wrong == "wasted work"


def test_note_details_passthrough():
    obj, err = parse_message_body("note", {"summary": "step 2 done", "details": "long form text"})
    assert err is None and obj.details == "long form text"


def test_note_details_truncates():
    from daemon.config import MAX_BODY_LEN
    obj, err = parse_message_body("note", {"summary": "ok", "details": "y" * (MAX_BODY_LEN + 10)})
    assert err is None and len(obj.details) == MAX_BODY_LEN


def test_question_field_truncates_to_max_body_len():
    from daemon.config import MAX_BODY_LEN
    obj, err = parse_message_body("question", {
        "question": "q" * (MAX_BODY_LEN + 10),
        "continuation_plan": "keep going",
    })
    assert err is None and len(obj.question) == MAX_BODY_LEN


def test_config_caps_values():
    from daemon.config import MSG_MAX_BODY, MAX_PROGRESS_NOTES, MAX_SUMMARY_LEN, MAX_BODY_LEN
    assert MSG_MAX_BODY == 256 * 1024
    assert MAX_PROGRESS_NOTES == 50
    assert MAX_SUMMARY_LEN == 280
    assert MAX_BODY_LEN == 4000


def test_non_string_required_field_rejected():
    obj, err = parse_message_body("note", {"summary": 123})
    assert obj is None and err == (400, "summary required")
