from daemon.hygiene import sanitize_answer


def test_strips_escape_and_newlines():
    assert sanitize_answer("1\x1b[2J\n") == "1"
    assert sanitize_answer("ye\rs\n\n") == "yes"


def test_strips_leading_slash_command():
    assert sanitize_answer("/exit") == "exit"
    assert sanitize_answer("  /quit now ") == "quit now"


def test_plain_answer_untouched():
    assert sanitize_answer("2") == "2"
