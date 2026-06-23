import time

from daemon.session import Session


class FakePty:
    def __init__(self, *a, **k):
        self.grid = "booting"
        self.written = []
        self.alive = True

    def spawn(self):
        pass

    def pump(self, timeout=0.1):
        time.sleep(0.01)
        return True

    def render(self):
        return self.grid

    def write(self, data):
        self.written.append(data)

    def is_alive(self):
        return self.alive

    def close(self):
        self.alive = False


class FakeDriver:
    def is_task_accepted_signal(self, grid):
        return "esc to interrupt" in grid

    def classify(self, grid, task_accepted):
        if "Do you want to proceed" in grid:
            return "waiting_for_user"
        if "esc to interrupt" in grid:
            return "working"
        return "done_candidate" if task_accepted else "idle"


def test_session_emits_waiting_then_resumes():
    fake = FakePty()
    s = Session(FakeDriver(), argv=["x"], env={}, cwd="/tmp",
                pty_factory=lambda argv, cwd, cols, rows, env: fake)
    s.start("do the thing")
    assert "do the thing" in "".join(fake.written)
    fake.grid = "esc to interrupt"
    time.sleep(0.1)
    fake.grid = "Do you want to proceed"
    evt = s.wait_event(0, 3)
    assert evt is not None and evt.kind == "waiting_for_user"
    assert s.respond(evt.event_id, "yes") is True
    assert "yes\n" in "".join(fake.written)
    s.stop()
