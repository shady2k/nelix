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
