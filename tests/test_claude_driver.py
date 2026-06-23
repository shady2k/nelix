from pathlib import Path

import pytest

from daemon.drivers import get_driver
from daemon.drivers.claude import ClaudeDriver

FIX = Path(__file__).parent / "fixtures"


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


def test_classify_working():
    assert ClaudeDriver().classify(_grid("claude_working.txt"), True) == "working"


def test_classify_waiting():
    assert ClaudeDriver().classify(_grid("claude_waiting.txt"), True) == "waiting_for_user"


def test_classify_idle_before_task_is_not_done():
    assert ClaudeDriver().classify(_grid("claude_idle.txt"), False) == "idle"


def test_classify_idle_after_task_is_done_candidate():
    assert ClaudeDriver().classify(_grid("claude_idle.txt"), True) == "done_candidate"


from daemon.drivers.claude import ClaudeDriver, ASK_MODE_TOGGLE


def test_ask_mode_detection():
    d = ClaudeDriver()
    assert ASK_MODE_TOGGLE == "\x1b[Z"
    assert d.is_ask_mode("... ⏵⏵ accept edits on (shift+tab to cycle)") is False
    assert d.is_ask_mode("... (shift+tab to cycle)  normal mode") is True
