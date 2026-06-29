"""Timestamped PTY capture for deterministic replay (spec §8).

The raw spool (`sessions/<id>/raw`) carries no timing, so a capture cannot be aligned to decision
times. This adds a parallel timed log: a sequence of length-prefixed records `<offset> <n>\\n` + n
bytes, where `offset` is seconds since the first record (read from an INJECTED clock — no wall clock
in tests). With timestamps any session is deterministically replayable AND alignable, and the same
capture becomes a ready test fixture (the FakeClock is advanced by the recorded inter-chunk deltas).
"""
import paths
from daemon.clock import WallClock


def _write_record(f, offset, data):
    # Length-prefixed so a chunk that itself contains newlines (or looks like a header) round-trips:
    # the reader consumes exactly `n` bytes after the header, never line-splitting the payload.
    f.write(f"{offset:.6f} {len(data)}\n".encode())
    f.write(data)


class CaptureWriter:
    """Append-only timed capture. `record(data)` stamps the chunk with offset = clock.now() - start."""

    def __init__(self, path, clock=None):
        self._clock = clock if clock is not None else WallClock()
        self._f = open(path, "wb", opener=paths.private_opener)   # 0600, same discipline as raw
        self._start = None

    def record(self, data):
        if not data:
            return
        now = self._clock.now()
        if self._start is None:
            self._start = now
        _write_record(self._f, now - self._start, data)
        self._f.flush()

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


def read_capture(src):
    """Yield (offset, bytes) records in order from a capture path or an open binary stream."""
    if hasattr(src, "read"):
        data = src.read()
    else:
        with open(src, "rb") as f:
            data = f.read()
    pos = 0
    n = len(data)
    while pos < n:
        nl = data.index(b"\n", pos)
        offset_s, len_s = data[pos:nl].split()
        offset = float(offset_s)
        count = int(len_s)
        start = nl + 1
        yield (offset, data[start:start + count])
        pos = start + count


def synthesize_capture(raw, out_path, *, chunk_size, dt):
    """Build a timed capture from a raw byte stream that has NO timestamps (the s-beb967e9 fixture):
    split `raw` into `chunk_size` slices and synthesize a per-chunk offset of `i * dt`. Lossless —
    concatenating the records reproduces `raw` exactly."""
    raw = bytes(raw)
    with open(out_path, "wb", opener=paths.private_opener) as f:
        for i, off in enumerate(range(0, len(raw), chunk_size)):
            _write_record(f, i * dt, raw[off:off + chunk_size])
