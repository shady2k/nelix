import select

import pyte
import ptyprocess


class PtySession:
    def __init__(self, argv, cwd=None, cols=120, rows=40, env=None):
        self._argv = argv
        self._cwd = cwd
        self._cols = cols
        self._rows = rows
        self._env = env
        self._child = None
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)

    def spawn(self):
        self._child = ptyprocess.PtyProcess.spawn(
            self._argv, dimensions=(self._rows, self._cols), cwd=self._cwd, env=self._env
        )

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
        self._stream.feed(data)
        return True

    def render(self):
        return "\n".join(self._screen.display)

    def write(self, data):
        if self._child is not None:
            self._child.write(data.encode())

    def is_alive(self):
        return self._child is not None and self._child.isalive()

    def close(self):
        if self._child is not None:
            self._child.close(force=True)
