import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.drivers.claude import ClaudeDriver  # noqa: E402


class Ctx:
    def __init__(self, stable_for=9.9, bytes_idle_for=9.9, child_alive=True, exit_code=None):
        self.stable_for = stable_for; self.bytes_idle_for = bytes_idle_for
        self.child_alive = child_alive; self.exit_code = exit_code


D = ClaudeDriver()


def test_working_when_interrupt_marker():
    assert D.classify("doing things… esc to interrupt", Ctx(stable_for=9.9)) == "working"


def test_idle_prompt_only_when_stable():
    frame = "Here is my answer.\n❯ "
    assert D.classify(frame, Ctx(stable_for=0.2)) == "quiet_working"   # box present but not settled
    assert D.classify(frame, Ctx(stable_for=2.0)) == "idle_prompt"     # settled -> stop


def test_permission_prompt():
    frame = "Proceed?\n 1. Yes\n 3. No\n❯ "
    assert D.classify(frame, Ctx(stable_for=2.0)) == "permission_prompt"


def test_crashed_and_exit_code():
    assert D.classify("Traceback (most recent call last):", Ctx()) == "crashed"
    assert D.classify("anything", Ctx(child_alive=False, exit_code=0)) == "exited"
    assert D.classify("anything", Ctx(child_alive=False, exit_code=2)) == "crashed"


def test_quiet_working_when_alive_no_markers():
    assert D.classify("compiling…", Ctx(stable_for=0.1)) == "quiet_working"


def test_normalize_frame_zeroes_spinner():
    a = D.normalize_frame("⠋ thinking 1.2s · 3 tokens\n❯ ")
    b = D.normalize_frame("⠙ thinking 4.8s · 9 tokens\n❯ ")
    assert a == b   # spinner/clock/counter differences erased -> semantically stable
