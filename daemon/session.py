import threading
import time

from daemon.hygiene import sanitize_answer


class Session:
    def __init__(self, session_id, executor, driver, launcher, spec, events,
                 cols=120, rows=40, logger=None):
        self._id = session_id
        self._executor = executor
        self._driver = driver
        self._launcher = launcher
        self._spec = spec
        self._events = events
        self._cols = cols
        self._rows = rows
        self._log = logger
        self._handle = None
        self._task_accepted = False
        self._state = "idle"
        self._last_state = None
        self._thread = None
        self._stop = threading.Event()

    def start(self, task):
        self._handle = self._launcher.start(self._spec, self._cols, self._rows)
        self._wait_until_ready()
        self._ensure_ask_mode()
        self._submit(task)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _submit(self, text):
        # The TUI treats CR (\r), not LF, as Enter. Type, let it render, then CR.
        self._handle.write(text)
        time.sleep(0.3)
        self._handle.write("\r")

    def _wait_until_ready(self, timeout=20.0, stable_for=1.2):
        last = None; stable_since = None
        deadline = time.time() + timeout
        while time.time() < deadline and self._handle.is_alive():
            self._handle.pump(0.1)
            grid = self._handle.render()
            if grid != last:
                last = grid; stable_since = time.time()
            elif grid.strip() and stable_since is not None and time.time() - stable_since >= stable_for:
                return

    def _ensure_ask_mode(self, attempts=4):
        # Cycle Shift+Tab until the driver reports ask-mode (not auto/plan).
        from daemon.drivers.claude import ASK_MODE_TOGGLE
        for _ in range(attempts):
            self._handle.pump(0.1)
            if self._driver.is_ask_mode(self._handle.render()):
                return
            self._handle.write(ASK_MODE_TOGGLE)
            time.sleep(0.3)

    def _loop(self):
        while not self._stop.is_set() and self._handle.is_alive():
            self._handle.pump(0.1)
            grid = self._handle.render()
            if not self._task_accepted and self._driver.is_task_accepted_signal(grid):
                self._task_accepted = True
            state = self._driver.classify(grid, self._task_accepted)
            if state in ("working", "waiting_for_user"):
                self._task_accepted = True
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
        evt = self._events.publish(self._id, self._executor, kind, summary, self._state)
        if self._log is not None:
            self._log.audit_decision(self._id, self._executor, kind, evt.event_id, grid)
        # publish() notifies the shared EventQueue condition — no per-session wait here.

    def respond(self, event_id, answer):
        pending = self._events.pending(self._id)
        if pending is None or pending.event_id != event_id:
            return False                      # bind to the current pending decision
        self._events.mark_answered(event_id)
        if self._handle is not None:
            self._submit(sanitize_answer(answer))
            self._last_state = None
        return True

    def snapshot(self):
        return {"session_id": self._id, "executor": self._executor,
                "state": self._state, "task_accepted": self._task_accepted}

    def stop(self):
        self._stop.set()
        if self._handle is not None:
            self._launcher.stop(self._handle)
