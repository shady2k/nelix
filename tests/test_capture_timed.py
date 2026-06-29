from daemon.capture import CaptureWriter, read_capture, synthesize_capture
from daemon.clock import FakeClock


def test_capture_records_monotonic_offsets_and_reads_back(tmp_path):
    clk = FakeClock(0.0)
    path = tmp_path / "s.capture"
    w = CaptureWriter(path, clock=clk)
    chunks = [b"hello", b" ", b"world\n", b"\x1b[2J\x1b[H", b"DONE\r\n"]
    for c in chunks:
        clk.advance(0.5)
        w.record(c)
    w.close()
    recs = list(read_capture(path))
    assert [c for _, c in recs] == chunks            # bytes preserved exactly, in order
    offsets = [o for o, _ in recs]
    assert offsets == sorted(offsets)                # monotonically non-decreasing
    assert offsets[0] == 0.0 and offsets[-1] > offsets[0]


def test_capture_handles_bytes_with_embedded_newlines_and_headers():
    # a chunk that itself looks like a header line must round-trip (length-prefixed, not line-split).
    import io
    from daemon.capture import _write_record
    buf = io.BytesIO()
    tricky = b"0.5 99\nnot a header\n"
    _write_record(buf, 1.25, tricky)
    buf.seek(0)
    recs = list(read_capture(buf))
    assert recs == [(1.25, tricky)]


def test_dialog_writes_timed_capture_alongside_raw(tmp_path):
    # The live capture path (Dialog.append_raw) records a timed capture driven by the injected clock,
    # keeping `raw` byte-exact for the renderer.
    from daemon.dialog import Dialog
    clk = FakeClock(0.0)
    d = Dialog(tmp_path / "s", tail_lines=10, spool_max_bytes=10_000_000, clock=clk)
    chunks = [b"first\r\n", b"\x1b[2Jsecond\r\n", b"third"]
    for c in chunks:
        clk.advance(0.25)
        d.append_raw(c)
    d.close()
    # raw stays byte-exact
    assert (tmp_path / "s" / "raw").read_bytes() == b"".join(chunks)
    # capture carries the same bytes with monotonic synthesized-from-clock offsets
    recs = list(read_capture(tmp_path / "s" / "capture"))
    assert [c for _, c in recs] == chunks
    assert [o for o, _ in recs] == [0.0, 0.25, 0.5]


def test_synthesize_capture_from_raw(tmp_path):
    raw = b"A" * 50 + b"B" * 50 + b"C" * 30          # 130 bytes
    out = tmp_path / "fix.capture"
    synthesize_capture(raw, out, chunk_size=50, dt=0.1)
    recs = list(read_capture(out))
    assert b"".join(c for _, c in recs) == raw       # lossless reconstruction of the raw stream
    offsets = [o for o, _ in recs]
    assert offsets == [0.0, 0.1, 0.2]                # synthesized per-chunk offsets
