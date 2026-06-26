import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.dialog import Dialog  # noqa: E402


def _mk(tmp_path, **kw):
    kw.setdefault("tail_lines", 100); kw.setdefault("spool_max_bytes", 1_000_000)
    return Dialog(tmp_path / "s1", **kw)


def test_session_dir_and_files_are_private(tmp_path):
    d = _mk(tmp_path)
    d.append_raw(b"secret-bearing output"); d.add_line("line")
    sdir = tmp_path / "s1"
    assert oct(sdir.stat().st_mode & 0o777) == "0o700"                       # 0700 session dir
    assert oct((sdir / "raw").stat().st_mode & 0o777) == "0o600"             # 0600 raw spool
    assert oct((sdir / "transcript.jsonl").stat().st_mode & 0o777) == "0o600"


def test_raw_spool_appends(tmp_path):
    d = _mk(tmp_path)
    d.append_raw(b"abc"); d.append_raw(b"def")
    assert (tmp_path / "s1" / "raw").read_bytes() == b"abcdef"


def test_lines_and_turns_indexing(tmp_path):
    d = _mk(tmp_path)
    assert d.current_turn() == 0
    d.add_line("a0"); d.add_line("a1")          # turn 0: lines 0,1
    d.mark_turn_boundary()                       # -> turn 1
    d.add_line("b0")                             # turn 1: line 2
    assert d.turn_count() == 2 and d.line_count() == 3 and d.current_turn() == 1
    assert d.turn_text(0)["text"] == "a0\na1"
    assert d.turn_text(1)["text"] == "b0"


def test_turn_text_pagination(tmp_path):
    d = _mk(tmp_path)
    for i in range(5):
        d.add_line(f"L{i}")                      # "L0\nL1\nL2\nL3\nL4" (len 14)
    full = d.turn_text(0)
    assert full["text"] == "L0\nL1\nL2\nL3\nL4" and full["truncated"] is False
    page = d.turn_text(0, offset=3, limit=5)
    assert page["text"] == "L1\nL2" and page["offset"] == 3 and page["truncated"] is True


def test_transcript_jsonl_persisted(tmp_path):
    d = _mk(tmp_path)
    d.add_line("x"); d.mark_turn_boundary(); d.add_line("y")
    recs = [json.loads(l) for l in (tmp_path / "s1" / "transcript.jsonl").read_text().splitlines()]
    assert recs == [{"idx": 0, "turn": 0, "text": "x"}, {"idx": 1, "turn": 1, "text": "y"}]


def test_range_text_for_frozen_event(tmp_path):
    d = _mk(tmp_path)
    for i in range(4):
        d.add_line(f"L{i}")
    assert d.range_text(1, 3)["text"] == "L1\nL2"   # [start, end)


def test_raw_cap_drops_oldest_and_marks_base(tmp_path):
    d = _mk(tmp_path, spool_max_bytes=4)
    d.append_raw(b"123456")                      # exceeds 4 -> keep last 4
    assert (tmp_path / "s1" / "raw").read_bytes() == b"3456"


def test_dialog_reader_reads_finished_transcript(tmp_path):
    from daemon.dialog import Dialog, DialogReader
    d = Dialog(tmp_path / "s-x", tail_lines=10, spool_max_bytes=10000)
    d.add_line("turn0 line0")
    d.mark_turn_boundary()
    d.add_line("turn1 line0")
    d.add_line("turn1 line1")
    d.close()
    r = DialogReader(tmp_path / "s-x")
    assert r.turn_count() == 2
    assert r.turn_text(0)["text"] == "turn0 line0"
    assert r.turn_text(1)["text"] == "turn1 line0\nturn1 line1"


def test_dialog_reader_missing_is_unavailable(tmp_path):
    from daemon.dialog import DialogReader
    r = DialogReader(tmp_path / "nope")
    assert r.turn_count() == 0
    assert r.turn_text(0)["unavailable"] is True
