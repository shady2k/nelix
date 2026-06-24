import select

import pyte
import ptyprocess


def _row_text(row):
    # pyte history rows are {col: Char}; display rows are already strings.
    if isinstance(row, str):
        return row.rstrip()
    return "".join(row[c].data for c in sorted(row)).rstrip()


class PtySession:
    def __init__(self, argv, cwd=None, cols=120, rows=40, env=None, dialog=None):
        self._argv = argv
        self._cwd = cwd
        self._cols = cols
        self._rows = rows
        self._env = env
        self._child = None
        self._dialog = dialog
        self._screen = pyte.HistoryScreen(cols, rows, history=100000, ratio=0.5)
        self._stream = pyte.ByteStream(self._screen)
        self._history_committed = 0   # how many top-history lines already added to the dialog

    def spawn(self):
        self._child = ptyprocess.PtyProcess.spawn(
            self._argv, dimensions=(self._rows, self._cols), cwd=self._cwd, env=self._env)

    def pump(self, timeout=0.1):
        if self._child is None:
            return False
        try:
            r, _, _ = select.select([self._child.fd], [], [], timeout)
        except (OSError, ValueError):
            return False  # fd closed (e.g. child exited or session torn down)
        if not r:
            return False
        try:
            data = self._child.read(65536)
        except (EOFError, OSError, ValueError):
            return False
        if self._dialog is not None:
            self._dialog.append_raw(data)
        self._stream.feed(data)
        self._commit_scrolled()
        return True

    def _commit_scrolled(self):
        # Lines that scrolled off the top of the normal buffer are final — commit each once.
        # Inert for alt-screen/repaint-only TUIs (history stays empty — Spike D); those rely on
        # flush_viewport() at each stop instead.
        top = list(self._screen.history.top)
        if self._dialog is not None and len(top) > self._history_committed:
            for row in top[self._history_committed:]:
                self._dialog.add_line(_row_text(row))
            self._history_committed = len(top)

    def flush_viewport(self, dialog):
        # Commit the current viewport's non-empty lines (final turn tail / fallback snapshot).
        for line in self._screen.display:
            t = line.rstrip()
            if t:
                dialog.add_line(t)

    def render(self):
        return "\n".join(self._screen.display)

    def write(self, data):
        if self._child is not None:
            self._child.write(data.encode())

    def is_alive(self):
        return self._child is not None and self._child.isalive()

    def exit_code(self):
        if self._child is None:
            return None
        return self._child.exitstatus

    def close(self):
        if self._child is not None:
            self._child.close(force=True)
