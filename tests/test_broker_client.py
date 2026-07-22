import os
import subprocess
import threading

import pytest

import daemon.broker_client as broker_client
from daemon.broker_client import BrokerClient, BrokerSpawnError, set_broker, get_broker


class _StubbornProc:
    """A broker that ignores the socket close and has to be killed. Stands in for a real one so
    the KILL path can be tested at all: a healthy broker exits on its own and never reaches it."""

    def __init__(self):
        self.returncode = None
        self.killed = False
        self.waits_after_kill = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.killed:
            raise subprocess.TimeoutExpired(cmd="broker", timeout=timeout)
        self.waits_after_kill += 1
        self.returncode = -9
        return self.returncode

    def kill(self):
        self.killed = True


def test_close_reaps_a_broker_it_had_to_kill():
    """kill() only SIGNALs — without a following wait() the broker stays a zombie.

    Every leaked child is invisible: nothing in this class logs a spawn or a reap, so a process
    that outlives its owner leaves no trace except a suite that will not finish. The macOS job
    wedged at 98% with the runner terminating leftover Python processes, and there was no record
    anywhere of who had created them.
    """
    bc = BrokerClient.__new__(BrokerClient)          # no real broker: only close()'s discipline
    bc._lock = threading.Lock()
    bc._sock = None
    bc._proc = _StubbornProc()

    bc.close()

    assert bc._proc is None or bc._proc.returncode is not None, \
        "close() killed the broker but never reaped it — that is a zombie"


def test_a_dead_broker_cannot_block_spawn_forever():
    """The client's socket must carry a recv deadline, on every platform.

    `spawn()` sends a request and then blocks in recv_msg for the reply — while holding
    `self._lock`, so anything that blocks it blocks every later spawn behind it too.

    On macOS a peer-closed AF_UNIX/SOCK_DGRAM wakes that recv with ECONNRESET, which
    broker_proto turns into EOFError, which `spawn()` catches to restart and retry once. Linux
    gives no such wakeup: the recv simply never returns. That asymmetry is not theoretical — it
    is the same one that hangs tests/test_broker_proto.py::test_eof_raises forever on Linux,
    which parked the whole CI suite at 96% until a cap killed it.

    So peer-close is not a portable liveness signal and must not be the only one. A deadline is,
    and it needs no new except clause: socket.timeout IS TimeoutError, which is an OSError, so
    `spawn()`'s existing `except (OSError, EOFError)` already routes it to restart-and-retry.
    """
    bc = BrokerClient()
    try:
        assert bc._sock.gettimeout() is not None, \
            "broker client socket has no recv deadline; a dead broker blocks spawn() forever on Linux"
    finally:
        bc.close()


def test_spawn_returns_live_master_and_pid(tmp_path):
    bc = BrokerClient()
    try:
        master, pid, pgid = bc.spawn(["cat"], str(tmp_path), dict(os.environ), 80, 24)
        assert pid == pgid and os.getpgid(pid) == pgid
        os.write(master, b"yo\n")
        import time; time.sleep(0.3)
        assert b"yo" in os.read(master, 4096)
        os.close(master); os.kill(pid, 9)
    finally:
        bc.close()


def test_spawn_failure_raises(tmp_path):
    bc = BrokerClient()
    try:
        with pytest.raises(BrokerSpawnError):
            bc.spawn(["/nope/nope"], str(tmp_path), dict(os.environ), 80, 24)
    finally:
        bc.close()


def test_lazy_respawn_after_broker_death(tmp_path):
    bc = BrokerClient()
    try:
        bc._proc.kill(); bc._proc.wait(timeout=5)        # simulate broker crash
        master, pid, pgid = bc.spawn(["cat"], str(tmp_path), dict(os.environ), 80, 24)
        assert os.getpgid(pid) == pgid                    # respawned transparently
        os.close(master); os.kill(pid, 9)
    finally:
        bc.close()


def test_spawn_ok_without_fd_raises(tmp_path, monkeypatch):
    # Protocol violation: status "ok" but no master fd. The client must not hand back a broken
    # handle (master=None) — it must raise instead.
    bc = BrokerClient()
    try:
        monkeypatch.setattr(broker_client, "send_msg", lambda *a, **k: None)
        monkeypatch.setattr(broker_client, "recv_msg",
                            lambda *a, **k: ({"status": "ok", "pid": 1, "pgid": 1}, None))
        with pytest.raises(BrokerSpawnError) as ei:
            bc.spawn(["cat"], str(tmp_path), dict(os.environ), 80, 24)
        assert ei.value.stage == "missing_fd"
    finally:
        bc.close()


def test_module_singleton():
    bc = BrokerClient()
    set_broker(bc)
    assert get_broker() is bc
    bc.close()
