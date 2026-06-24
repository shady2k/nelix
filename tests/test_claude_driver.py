import pytest

from daemon.drivers import get_driver
from daemon.drivers.claude import ClaudeDriver


@pytest.fixture
def driver():
    return ClaudeDriver()


def test_registry_returns_claude_driver():
    assert isinstance(get_driver("claude"), ClaudeDriver)


def test_register_decorator_adds_driver():
    from daemon.drivers import register, DRIVERS, get_driver

    @register("dummy")
    class Dummy:
        pass

    try:
        assert isinstance(get_driver("dummy"), Dummy)
    finally:
        DRIVERS.pop("dummy", None)


def test_ask_mode_toggle_and_detection(driver):
    assert driver.ask_mode_toggle == "\x1b[Z"
    assert driver.is_ask_mode("... ⏵⏵ accept edits on (shift+tab to cycle)") is False
    assert driver.is_ask_mode("... (shift+tab to cycle)  normal mode") is True
