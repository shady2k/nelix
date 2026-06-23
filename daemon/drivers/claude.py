WORKING_MARKERS = ("esc to interrupt",)
WAITING_MARKERS = ("Do you want to proceed", "❯ 1.", "1. Yes")
CRASH_MARKERS = ("Traceback (most recent call last)", "command not found",
                 "Invalid API key", "authentication_error")
INPUT_BOX_MARKERS = ("│ >", "> ")


class ClaudeDriver:
    def is_task_accepted_signal(self, grid):
        return any(m in grid for m in WORKING_MARKERS)

    def classify(self, grid, task_accepted):
        if any(m in grid for m in CRASH_MARKERS):
            return "crashed"
        if any(m in grid for m in WAITING_MARKERS):
            return "waiting_for_user"
        if any(m in grid for m in WORKING_MARKERS):
            return "working"
        if any(m in grid for m in INPUT_BOX_MARKERS):
            return "done_candidate" if task_accepted else "idle"
        return "idle"
