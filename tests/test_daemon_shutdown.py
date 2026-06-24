"""TDD: SIGTERM handler in daemon/app.py must call stop_all() then SystemExit."""
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_install_shutdown_handler_calls_stop_all_and_exits():
    """Handler must call manager.stop_all() exactly once and raise SystemExit(0)."""
    from daemon.app import install_shutdown_handler

    class FakeManager:
        def __init__(self):
            self.stop_all_calls = 0

        def stop_all(self):
            self.stop_all_calls += 1

    manager = FakeManager()

    # Save and restore the prior SIGTERM disposition to avoid leaking global state.
    prior = signal.getsignal(signal.SIGTERM)
    try:
        handler = install_shutdown_handler(manager)
        # Verify the handler was installed.
        assert signal.getsignal(signal.SIGTERM) is handler

        # Calling the handler directly must stop_all then raise SystemExit(0).
        import pytest
        with pytest.raises(SystemExit) as exc_info:
            handler(signal.SIGTERM, None)

        assert exc_info.value.code == 0
        assert manager.stop_all_calls == 1
    finally:
        signal.signal(signal.SIGTERM, prior)
