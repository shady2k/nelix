import threading
import time

from daemon.events import EventQueue
from daemon.pty_session import PtySession


def _default_pty_factory(argv, cwd, cols, rows, env):
    return PtySession(argv, cwd=cwd, cols=cols, rows=rows, env=env)


class Session:
    def __init__(self, driver, argv, env, cwd, cols=120, rows=40,
                 pty_factory=_default_pty_factory):
        self._driver = driver
        self._argv = argv
        self._env = env
        self._cwd = cwd
        self._cols = cols
        self._rows = rows
        self._pty_factory = pty_factory
        self._pty = None
        self._events = EventQueue()
        self._task_accepted = False
        self._state = "idle"
        self._last_state = None
        self._cv = threading.Condition()
        self._thread = None
        self._stop = threading.Event()

    def start(self, task):
        self._pty = self._pty_factory(self._argv, self._cwd, self._cols, self._rows, self._env)
        self._pty.spawn()
        for _ in range(50):
            self._pty.pump(0.1)
            if self._pty.render().strip():
                break
        self._pty.write(task + "\n")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set() and self._pty.is_alive():
            self._pty.pump(0.1)
            grid = self._pty.render()
            if not self._task_accepted and self._driver.is_task_accepted_signal(grid):
                self._task_accepted = True
            state = self._driver.classify(grid, self._task_accepted)
            self._state = state
            if state != self._last_state:
                self._last_state = state
                if state == "waiting_for_user":
                    self._emit("waiting_for_user", grid)
                elif state == "done_candidate":
                    self._emit("done", grid)
                elif state == "crashed":
                    self._emit("crashed", grid)

    def _emit(self, kind, grid):
        summary = "\n".join(grid.strip().splitlines()[-8:])
        with self._cv:
            self._events.publish(kind, summary, self._state)
            self._cv.notify_all()

    def wait_event(self, after_seq, timeout):
        deadline = time.time() + timeout
        with self._cv:
            while True:
                evt = self._events.latest_after(after_seq)
                if evt is not None:
                    return evt
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)

    def respond(self, event_id, answer):
        ok = self._events.mark_answered(event_id)
        if ok and self._pty is not None:
            self._pty.write(answer + "\n")
            self._last_state = None
        return ok

    def snapshot(self):
        return {"state": self._state, "task_accepted": self._task_accepted}

    def stop(self):
        self._stop.set()
        if self._pty is not None:
            self._pty.close()
