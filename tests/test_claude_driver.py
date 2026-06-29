import pytest

from daemon.drivers import get_driver
from daemon.drivers.claude import ClaudeDriver
from daemon.observation import ObservationCtx

_CTX = ObservationCtx(last_submitted_text=None, child_alive=True, exit_code=None)


@pytest.fixture
def driver():
    return ClaudeDriver()


def test_registry_returns_claude_driver():
    assert isinstance(get_driver("claude"), ClaudeDriver)


def test_register_decorator_adds_driver():
    from daemon.drivers import register, DRIVERS, get_driver

    @register("dummy")
    class Dummy:                                  # a conforming stub (registry fails closed otherwise)
        ask_mode_toggle = ""
        command_prefixes = ()
        submit_key = "\r"
        def normalize_frame(self, f): return f
        def observe(self, f, ctx): return None
        def is_transcript_volatile(self, r): return False
        def format_submission(self, t): return t
        def submit_text(self, t): return t
        def select_option(self, i): return i
        def interrupt(self): return "\x1b"

    try:
        assert isinstance(get_driver("dummy"), Dummy)
        assert "dummy" in DRIVERS
    finally:
        DRIVERS.pop("dummy", None)


def test_ask_mode_toggle_and_detection(driver):
    # is_ask_mode is folded into observe() as the ask_mode field (spec §5.6).
    assert driver.ask_mode_toggle == "\x1b[Z"
    assert driver.observe("... ⏵⏵ accept edits on (shift+tab to cycle)", _CTX).ask_mode is False
    assert driver.observe("... (shift+tab to cycle)  normal mode", _CTX).ask_mode is True
