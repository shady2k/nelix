import os
import time

from daemon.pty_session import PtySession


def _spawn(argv, cwd=None, cols=80, rows=24):
    """Build a live PTY pair + child the way the broker does (setsid -> own group leader,
    pgid == pid), but with a single-threaded openpty()+fork() that is safe inside a test.
    Returns (master_fd, pid, pgid)."""
    master, slave = os.openpty()
    pid = os.fork()
    if pid == 0:
        os.setsid()
        if cwd:
            try:
                os.chdir(cwd)
            except OSError:
                pass
        os.dup2(slave, 0); os.dup2(slave, 1); os.dup2(slave, 2)
        os.close(master); os.close(slave)
        os.execvpe(argv[0], argv, os.environ.copy())
        os._exit(127)
    os.close(slave)
    # setsid races a plain fork(); wait (best-effort) until it takes effect so is_alive()'s
    # getpgid(pid) == pgid check is reliable. pgid == pid by the setsid contract (what the
    # broker reports), so return pid directly -- a fast-exiting child may already be gone.
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            if os.getpgid(pid) == pid:
                break
        except OSError:
            break
        time.sleep(0.005)
    return master, pid, pid


def _reap(pid):
    try:
        os.kill(pid, 9); os.waitpid(pid, 0)
    except OSError:
        pass


def test_render_raw_matches_pty_session_render():
    # render_raw is the SHARED renderer (capture tool + daemon must never drift): feeding the same
    # bytes must produce exactly what PtySession.render() yields, with no live child.
    from daemon.pty_session import render_raw
    data = b"hello\r\nworld\r\n\x1b[1mbold\x1b[0m"
    p = PtySession(None, 0, 0, cols=80, rows=24)    # pure: no fd, just feed bytes
    p._feed(data)                                   # pure: no spawn, no dialog
    assert render_raw(data, cols=80, rows=24) == p.render()
    assert "hello" in render_raw(data, 80, 24) and "world" in render_raw(data, 80, 24)


def test_render_raw_defaults_match_session_dims():
    # default 120x40 mirrors Session's defaults, so a session's raw replays at the right size.
    from daemon.pty_session import render_raw
    out = render_raw(b"line-a\r\nline-b")
    assert "line-a" in out and "line-b" in out
    assert len(out.split("\n")) == 40              # rows=40 viewport


def test_render_no_stray_kitty_u_native():
    # nelix-quv was a pyte artifact. A faithful engine consumes kitty-keyboard CSI natively,
    # including a sequence split across two _feed() calls (parser state carries). No pre-filter.
    s = PtySession(None, 0, 0, cols=80, rows=24)
    s._feed(b"\x1b[H\x1b[<u\x1b[>1u")
    assert not s.render().splitlines()[0].startswith("u")
    s2 = PtySession(None, 0, 0, cols=80, rows=24)
    s2._feed(b"\x1b[H\x1b[<"); s2._feed(b"u\x1b[>1u")
    assert not s2.render().splitlines()[0].startswith("u")


def test_render_keeps_real_u_text():
    from daemon.pty_session import render_raw
    assert "menu" in render_raw(b"menu")


def test_render_captures_child_output():
    master, pid, pgid = _spawn(["printf", "HELLO-NELIX\\n"], cols=40, rows=10)
    s = PtySession(master, pid, pgid, cols=40, rows=10)
    try:
        deadline = time.time() + 5
        while time.time() < deadline and s.is_alive():
            s.pump(0.1)
        s.pump(0.1)
        assert "HELLO-NELIX" in s.render()
    finally:
        s.close()
        _reap(pid)


def test_pump_tees_raw_and_flushes_viewport(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from daemon.dialog import Dialog
    d = Dialog(tmp_path / "s", tail_lines=100, spool_max_bytes=1_000_000)
    master, pid, pgid = _spawn(
        ["/bin/sh", "-c", "for i in 1 2 3 4 5 6; do echo line$i; done; sleep 0.2"],
        cols=40, rows=4)
    s = PtySession(master, pid, pgid, cols=40, rows=4, dialog=d)
    try:
        for _ in range(40):
            s.pump(0.1)
        s.finalize()
        raw = (tmp_path / "s" / "raw").read_bytes()
        assert b"line1" in raw and b"line6" in raw            # raw tees EVERYTHING
        # Transcript content is committed by TranscriptBuilder via ESU frames (Phase 2).
    finally:
        s.close()
        _reap(pid)


def test_feed_segments_at_esu_and_finalize_commits_tail():
    from daemon.pty_session import PtySession

    class _Sink:
        def __init__(self): self.frames = []; self.finals = 0
        def observe(self, frame): self.frames.append([r for r in frame.rows if r.strip()])
        def finalize(self, frame=None):
            if frame is not None: self.observe(frame)
            self.finals += 1

    sink = _Sink()
    s = PtySession(None, 0, 0, cols=40, rows=6, transcript=sink)
    ESU = b"\x1b[?2026l"
    # two complete frames in ONE chunk: "AAA"<ESU> then home+"BBB"<ESU>
    s._feed(b"\x1b[HAAA" + ESU + b"\x1b[H\x1b[2JBBB" + ESU)
    assert len(sink.frames) == 2                       # snapshot per ESU, not collapsed to one
    assert any("AAA" in "".join(f) for f in sink.frames[:1])
    assert any("BBB" in "".join(f) for f in sink.frames)
    s.finalize()
    assert sink.finals == 1


def test_feed_carries_partial_esu_across_chunks():
    from daemon.pty_session import PtySession

    class _Sink:
        def __init__(self): self.n = 0
        def observe(self, frame): self.n += 1
        def finalize(self, frame=None): pass

    sink = _Sink()
    s = PtySession(None, 0, 0, cols=40, rows=6, transcript=sink)
    s._feed(b"\x1b[HZZZ\x1b[?2026")     # ESU split across reads: no frame yet
    assert sink.n == 0
    s._feed(b"l")                        # completes the ESU -> exactly one frame
    assert sink.n == 1


def test_leader_status_clean_exit():
    # fd-backed sessions have NO waitpid status: a clean exit is reported as dead-without-status
    # (status_available is False, exit_code/signal None). Exit-code/signal classification of
    # _exit_kind is covered separately in test_session_exit_kind.py with status_available=True.
    master, pid, pgid = _spawn(["true"])           # exits 0 immediately
    s = PtySession(master, pid, pgid)
    try:
        deadline = time.time() + 5
        while time.time() < deadline and s.is_alive():
            s.pump(0.05)
        st = s.leader_status()
        assert st.alive is False and st.exit_code is None
        assert st.signal is None and st.status_available is False
    finally:
        s.close()
        _reap(pid)


def test_leader_status_signal_death():
    import signal
    master, pid, pgid = _spawn(["sleep", "30"])
    s = PtySession(master, pid, pgid)
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)                          # reap so kill(pid,0) fails -> dead
        deadline = time.time() + 5
        while time.time() < deadline and s.is_alive():
            s.pump(0.1)
        st = s.leader_status()
        # No waitpid in the fd model -> a signal death is reported dead-without-status (not signal=9).
        assert st.alive is False and st.signal is None and st.status_available is False
    finally:
        s.close()


def test_leader_status_defensive_when_status_unavailable():
    from daemon.launchers.base import LeaderStatus
    # fd-backed sessions NEVER expose waitpid status: status_available is always False.
    s = PtySession(None, 0, 0)                      # no fd -> is_alive() False
    st = s.leader_status()
    assert st == LeaderStatus(alive=False, exit_code=None, signal=None, status_available=False)


def test_leader_pgid_matches_setsid_leader():
    master, pid, pgid = _spawn(["sleep", "5"])
    s = PtySession(master, pid, pgid)
    try:
        assert s.leader_pid() == s.leader_pgid()   # setsid -> own group leader (pid == pgid)
        assert s.leader_pgid() == os.getpgid(s.leader_pid())
    finally:
        s.close()
        _reap(pid)


def test_real_spawn_leader_is_group_leader():
    master, pid, pgid = _spawn(["/bin/sh", "-c", "sleep 5"], cwd="/tmp", cols=80, rows=24)
    p = PtySession(master, pid, pgid, cols=80, rows=24)
    try:
        p.assert_leader_is_group_leader()             # must not raise: pid == pgid (setsid)
        assert p.leader_pid() == p.leader_pgid()
    finally:
        p.close()
        _reap(pid)
