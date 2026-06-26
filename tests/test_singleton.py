import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon import singleton  # noqa: E402


def test_acquire_excludes_second_holder_and_exposes_metadata(tmp_path):
    lock = tmp_path / "daemon.lock"
    fd = singleton.acquire(lock, {"pid": 111, "start_fingerprint": "fp1", "port": 8765})
    assert fd is not None
    assert singleton.read_holder(lock) == {"pid": 111, "start_fingerprint": "fp1", "port": 8765}
    # second acquire on the SAME lock path fails while the first fd is open
    assert singleton.acquire(lock, {"pid": 222}) is None
    import os
    os.close(fd)                                  # release
    fd2 = singleton.acquire(lock, {"pid": 333})   # now free
    assert fd2 is not None
    os.close(fd2)


def test_read_holder_missing_is_none(tmp_path):
    assert singleton.read_holder(tmp_path / "nope.lock") is None
