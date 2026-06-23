import time
from conftest import EXECUTOR, make_spec
from daemon.events import EventQueue
from daemon.session import Session


class FakePty:
    def __init__(self, *a, **k):
        self.grid = "booting"; self.written = []; self.alive = True
    def pump(self, timeout=0.1): time.sleep(0.01); return True
    def render(self): return self.grid
    def write(self, data): self.written.append(data)
    def is_alive(self): return self.alive
    def close(self): self.alive = False


class FakeLauncher:
    def __init__(self, pty): self._pty = pty
    capabilities = None
    def start(self, spec, cols=120, rows=40): return self._pty
    def stop(self, handle): handle.close()


class FakeDriver:
    ask_mode_toggle = "\x1b[Z"
    def is_task_accepted_signal(self, grid): return "esc to interrupt" in grid
    def is_ask_mode(self, grid): return "ASKMODE" in grid
    def classify(self, grid, task_accepted):
        if "Do you want to proceed" in grid: return "waiting_for_user"
        if "esc to interrupt" in grid: return "working"
        return "done_candidate" if task_accepted else "idle"


def test_session_emits_with_session_id_then_resumes():
    fake = FakePty(); fake.grid = "ASKMODE ❯"  # ready + ask-mode so startup proceeds
    q = EventQueue()
    s = Session("s1", EXECUTOR, FakeDriver(), FakeLauncher(fake), make_spec(), q)
    s.start("do the thing")
    assert "do the thing" in "".join(fake.written)
    fake.grid = "esc to interrupt"; time.sleep(0.1)
    fake.grid = "Do you want to proceed"
    evt = q.wait_event(0, 3)        # read via the shared queue (Session has no wait_event)
    assert evt is not None and evt.kind == "waiting_for_user" and evt.session_id == "s1"
    assert evt.executor == EXECUTOR
    # respond is bound to the current pending event_id
    assert s.respond("evt-bogus", "yes") is False
    assert s.respond(evt.event_id, "/yes\n") is True
    joined = "".join(fake.written)
    assert "yes" in joined and "\r" in joined and "/yes" not in joined  # hygiene applied
    s.stop()


def test_ensure_ask_mode_writes_driver_toggle():
    fake = FakePty(); fake.grid = "normal mode, no askmode marker"
    drv = FakeDriver(); drv.ask_mode_toggle = "\x1bSENTINEL"
    s = Session("s1", EXECUTOR, drv, FakeLauncher(fake), make_spec(), EventQueue())
    s._handle = fake
    s._ensure_ask_mode(attempts=2)
    assert "\x1bSENTINEL" in "".join(fake.written)
