import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAP = ROOT / "bin" / "nelix-capture"


def _run(*args):
    return subprocess.run([sys.executable, str(CAP), *args],
                          capture_output=True, text=True, timeout=20)


def test_capture_final_renders_last_screen(tmp_path):
    # --final = renderer state after ALL bytes. A clear sequence wipes earlier output, so the final
    # frame shows DONE and not the cleared "loading" line — proving faithful rendering.
    raw = tmp_path / "raw"
    raw.write_bytes("loading...\r\n\x1b[2J\x1b[HDONE\r\n❯ ".encode())
    r = _run(str(raw), "--final")
    assert r.returncode == 0
    assert "DONE" in r.stdout and "loading" not in r.stdout


def test_capture_at_marker_returns_a_frame_with_the_marker(tmp_path):
    raw = tmp_path / "raw"
    raw.write_bytes(b"step-one\r\nstep-two\r\n")
    r = _run(str(raw), "--at-marker", "step-one")
    assert r.returncode == 0 and "step-one" in r.stdout


def test_capture_at_marker_miss_is_nonzero_with_message(tmp_path):
    raw = tmp_path / "raw"
    raw.write_bytes(b"hello world\r\n")
    r = _run(str(raw), "--at-marker", "NOSUCHMARKER")
    assert r.returncode == 1
    assert "not found" in r.stderr and "bytes" in r.stderr   # reports it may be truncated


def test_capture_all_emits_distinct_candidates(tmp_path):
    raw = tmp_path / "raw"
    raw.write_bytes(b"AAAA" * 40 + b"\r\n" + b"BBBB" * 40 + b"\r\n")
    r = _run(str(raw), "--all")
    assert r.returncode == 0
    assert "AAAA" in r.stdout and "BBBB" in r.stdout


def test_capture_reads_dims_from_session_meta(tmp_path):
    # Given a session DIR, dims come from meta.json (replaying at the wrong size reflows wrongly).
    sd = tmp_path / "s-x"
    sd.mkdir()
    (sd / "raw").write_bytes(b"abc\r\n")
    (sd / "meta.json").write_text(json.dumps(
        {"cols": 80, "rows": 10, "executor": "zai", "driver": "claude"}))
    r = _run(str(sd), "--final")
    assert r.returncode == 0 and "abc" in r.stdout
    assert "80x10" in r.stderr                            # dims discovered from meta.json


def test_capture_malformed_meta_falls_back_to_defaults(tmp_path):
    # a hand-broken meta.json (null/non-numeric dims) must not crash the tool — fall back to 120x40.
    sd = tmp_path / "s-bad"
    sd.mkdir()
    (sd / "raw").write_bytes(b"abc\r\n")
    (sd / "meta.json").write_text('{"cols": null, "rows": "oops"}')
    r = _run(str(sd), "--final")
    assert r.returncode == 0 and "abc" in r.stdout
    assert "120x40" in r.stderr


def test_capture_dims_override_and_default(tmp_path):
    raw = tmp_path / "raw"
    raw.write_bytes(b"hi\r\n")
    assert "100x30" in _run(str(raw), "--cols", "100", "--rows", "30").stderr  # explicit override
    assert "120x40" in _run(str(raw)).stderr                                   # default dims
