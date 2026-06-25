import time

from daemon.pty_session import PtySession


def test_render_captures_child_output():
    s = PtySession(["printf", "HELLO-NELIX\\n"], cols=40, rows=10)
    s.spawn()
    deadline = time.time() + 5
    while time.time() < deadline and s.is_alive():
        s.pump(0.1)
    s.pump(0.1)
    assert "HELLO-NELIX" in s.render()
    s.close()


def test_pump_tees_raw_and_commits_scrolled_lines(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from daemon.pty_session import PtySession
    from daemon.dialog import Dialog
    d = Dialog(tmp_path / "s", tail_lines=100, spool_max_bytes=1_000_000)
    # Echo more lines than the 4-row screen so the top scrolls off into history.
    s = PtySession(["/bin/sh", "-c", "for i in 1 2 3 4 5 6; do echo line$i; done; sleep 0.2"],
                   cols=40, rows=4, dialog=d)
    s.spawn()
    for _ in range(40):
        s.pump(0.1)
    s.flush_viewport(d)
    raw = (tmp_path / "s" / "raw").read_bytes()
    assert b"line1" in raw and b"line6" in raw            # raw has everything
    joined = d.turn_text(0)["text"]
    assert "line1" in joined and "line6" in joined         # transcript preserved beyond the viewport
    s.close()


def test_leader_status_clean_exit():
    s = PtySession(["true"])           # exits 0 immediately
    s.spawn()
    while s.is_alive():
        s.pump(0.05)
    st = s.leader_status()
    assert st.alive is False and st.exit_code == 0 and st.status_available is True
    s.close()


def test_leader_status_signal_death():
    import os, signal
    s = PtySession(["sleep", "30"])
    s.spawn()
    os.kill(s.leader_pid(), signal.SIGKILL)
    time.sleep(0.2)
    st = s.leader_status()
    assert st.alive is False and st.signal == signal.SIGKILL and st.status_available is True
    s.close()


def test_leader_status_defensive_when_status_unavailable():
    from daemon.launchers.base import LeaderStatus

    class _StubChild:
        pid = 12345
        exitstatus = None
        signalstatus = None
        def isalive(self):
            return False

    s = PtySession(["true"])
    s._child = _StubChild()            # dead but no status populated after isalive()
    st = s.leader_status()
    assert st == LeaderStatus(alive=False, exit_code=None, signal=None, status_available=False)


def test_leader_pgid_matches_setsid_leader():
    import os
    s = PtySession(["sleep", "5"])
    s.spawn()
    assert s.leader_pgid() == os.getpgid(s.leader_pid())
    s.close()
