from daemon.launchers.base import LeaderStatus
from daemon.session import Session


def _exit_kind(status):
    # _exit_kind is a pure method that ignores self; call it unbound with None self.
    return Session._exit_kind.__get__(object())(status)


def test_no_status_is_neutral_exited():
    # Broker-backed: dead, no waitpid status. Must NOT be reported as crashed.
    s = LeaderStatus(alive=False, exit_code=None, signal=None, status_available=False)
    assert _exit_kind(s) == ("done", "exited")


def test_clean_exit_zero_still_exited():
    s = LeaderStatus(alive=False, exit_code=0, signal=None, status_available=True)
    assert _exit_kind(s) == ("done", "exited")


def test_signal_is_crashed():
    s = LeaderStatus(alive=False, exit_code=None, signal=9, status_available=True)
    assert _exit_kind(s) == ("crashed", "crashed")


def test_nonzero_exit_is_crashed():
    s = LeaderStatus(alive=False, exit_code=3, signal=None, status_available=True)
    assert _exit_kind(s) == ("crashed", "crashed")
