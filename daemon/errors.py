"""Shared daemon exceptions. Zero imports on purpose — safe to import from any layer
(pty_session, session) without triggering package __init__ import cycles."""


class PtyWriteTimeout(Exception):
    """Raised by a handle's write() when `data` could not be fully written before the
    deadline — e.g. the executor stopped draining its stdin and the PTY input buffer
    filled. Prevents a blocking write from wedging the monitor thread forever."""

    def __init__(self, written, total):
        super().__init__(f"wrote {written}/{total} bytes before write timeout")
        self.written = written
        self.total = total
