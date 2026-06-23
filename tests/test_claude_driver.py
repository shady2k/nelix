from pathlib import Path

import pytest

from daemon.drivers import get_driver
from daemon.drivers.claude import ClaudeDriver

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def driver():
    return ClaudeDriver()


def _grid(name):
    p = FIX / name
    if not p.exists():
        pytest.skip(f"fixture {name} not captured (run spike P0-B)")
    return p.read_text()


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


def test_classify_working(driver):
    assert driver.classify(_grid("claude_working.txt"), True) == "working"


def test_classify_waiting(driver):
    assert driver.classify(_grid("claude_waiting.txt"), True) == "waiting_for_user"


def test_classify_idle_before_task_is_not_done(driver):
    assert driver.classify(_grid("claude_idle.txt"), False) == "idle"


def test_classify_idle_after_task_is_done_candidate(driver):
    assert driver.classify(_grid("claude_idle.txt"), True) == "done_candidate"


def test_ask_mode_toggle_and_detection(driver):
    assert driver.ask_mode_toggle == "\x1b[Z"
    assert driver.is_ask_mode("... ⏵⏵ accept edits on (shift+tab to cycle)") is False
    assert driver.is_ask_mode("... (shift+tab to cycle)  normal mode") is True
