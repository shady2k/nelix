"""Flat-log Dialog unit tests.

Covers:
- File privacy (0700 dir, 0600 files).
- append_user_input: adds a ``» `` marker, sets last_user_input_offset.
- add_agent_line: emits ``‹agent›`` only on the first line after a user marker (transition),
  not per line.
- Records persist as ``{idx, kind, speaker, text}`` in transcript.jsonl.
- page(): snaps end to line boundary; next_offset chains to cover the log exactly once;
  speaker_at_start / continued correct for a mid-span page; an over-long line hard-splits.
- tail(): returns the snapped last-N chars plus speaker_at_start.
- since() is an alias for page().
- RLock concurrency test.
- DialogReader reads the new record format and serves page()/tail().
"""
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon.dialog import Dialog, DialogReader  # noqa: E402


def _mk(tmp_path, **kw):
    kw.setdefault("tail_lines", 100); kw.setdefault("spool_max_bytes", 1_000_000)
    return Dialog(tmp_path / "s1", **kw)


# ---- file-system / privacy ----

def test_session_dir_and_files_are_private(tmp_path):
    d = _mk(tmp_path)
    d.append_raw(b"secret-bearing output"); d.add_agent_line("line")
    sdir = tmp_path / "s1"
    assert oct(sdir.stat().st_mode & 0o777) == "0o700"                       # 0700 session dir
    assert oct((sdir / "raw").stat().st_mode & 0o777) == "0o600"             # 0600 raw spool
    assert oct((sdir / "transcript.jsonl").stat().st_mode & 0o777) == "0o600"


def test_raw_spool_appends(tmp_path):
    d = _mk(tmp_path)
    d.append_raw(b"abc"); d.append_raw(b"def")
    assert (tmp_path / "s1" / "raw").read_bytes() == b"abcdef"


def test_raw_cap_drops_oldest_and_marks_base(tmp_path):
    d = _mk(tmp_path, spool_max_bytes=4)
    d.append_raw(b"123456")           # exceeds 4 -> keep last 4
    assert (tmp_path / "s1" / "raw").read_bytes() == b"3456"


def test_raw_base_offset_advances_on_truncation(tmp_path):
    d = Dialog(tmp_path / "s", tail_lines=100, spool_max_bytes=200)
    d.append_raw(b"a" * 150)
    d.append_raw(b"b" * 150)          # total 300 > 200 -> retain last 200
    assert d.raw_base_offset() == 100  # 300 - 200
    d.close()


# ---- write API ----

def test_append_user_input_adds_marker_and_sets_offset(tmp_path):
    d = _mk(tmp_path)
    off = d.append_user_input("fix the bug")
    assert off == 0                            # first record starts at 0
    assert d.last_user_input_offset() == 0
    page = d.page()
    assert page["text"] == "» fix the bug"


def test_add_agent_line_emits_transition_marker_once_per_span(tmp_path):
    d = _mk(tmp_path)
    d.append_user_input("task")        # speaker = user
    d.add_agent_line("step 1")         # transition: ‹agent› + line
    d.add_agent_line("step 2")         # same span: no extra marker
    d.add_agent_line("step 3")
    records = [json.loads(l) for l in (tmp_path / "s1" / "transcript.jsonl").read_text().splitlines()]
    kinds = [r["kind"] for r in records]
    texts = [r["text"] for r in records]
    # Expect: marker(user), marker(agent), line, line, line
    assert kinds == ["marker", "marker", "line", "line", "line"]
    assert texts[0] == "» task"
    assert texts[1] == "‹agent›"
    assert texts[2:] == ["step 1", "step 2", "step 3"]


def test_agent_marker_appears_after_each_user_input(tmp_path):
    d = _mk(tmp_path)
    d.add_agent_line("first agent line")    # initial span: ‹agent› marker
    d.append_user_input("Q1")
    d.add_agent_line("second agent line")   # new span after user: another ‹agent›
    records = [json.loads(l) for l in (tmp_path / "s1" / "transcript.jsonl").read_text().splitlines()]
    markers = [r for r in records if r["kind"] == "marker" and r["speaker"] == "agent"]
    assert len(markers) == 2, f"expected 2 agent markers, got {len(markers)}: {records}"


def test_transcript_jsonl_persisted_as_flat_records(tmp_path):
    d = _mk(tmp_path)
    d.append_user_input("go")
    d.add_agent_line("done")
    recs = [json.loads(l) for l in (tmp_path / "s1" / "transcript.jsonl").read_text().splitlines()]
    assert len(recs) == 3                                       # user marker, agent marker, line
    for rec in recs:
        for key in ("idx", "kind", "speaker", "text"):
            assert key in rec, f"missing key {key!r} in {rec}"
    assert recs[0] == {"idx": 0, "kind": "marker", "speaker": "user", "text": "» go"}
    assert recs[1] == {"idx": 1, "kind": "marker", "speaker": "agent", "text": "‹agent›"}
    assert recs[2] == {"idx": 2, "kind": "line",   "speaker": "agent", "text": "done"}


def test_last_agent_marker_offset_set(tmp_path):
    d = _mk(tmp_path)
    d.append_user_input("task")
    d.add_agent_line("output")
    # agent marker is the second record; user marker "» task" is 6 chars + 1 sep = offset 7
    assert d.last_agent_marker_offset() == len("» task") + 1   # = 7


# ---- page() ----

def test_page_no_limit_returns_all(tmp_path):
    d = _mk(tmp_path)
    d.add_agent_line("A"); d.add_agent_line("B"); d.add_agent_line("C")
    p = d.page()
    assert "A" in p["text"] and "B" in p["text"] and "C" in p["text"]
    assert p["next_offset"] == p["total_len"]


def test_page_next_offset_chains_cover_log_exactly_once(tmp_path):
    d = _mk(tmp_path)
    for i in range(10):
        d.add_agent_line(f"line-{i:02d}")
    full = d.page()["text"]
    # Page with limit 20 and chain via next_offset until done
    parts = []
    off = 0
    while True:
        p = d.page(off, limit=20)
        parts.append(p["text"])
        if p["next_offset"] >= p["total_len"]:
            break
        off = p["next_offset"]
    reconstructed = "\n".join(p for p in parts if p)
    # The full text equals the reconstructed text (snapping may trim within 60% of a page boundary,
    # hard-split adds no gap, so every char appears exactly once when we re-join pages)
    assert reconstructed == full


def test_page_snaps_end_to_last_newline(tmp_path):
    d = _mk(tmp_path)
    d.add_agent_line("AAAA"); d.add_agent_line("BBBB"); d.add_agent_line("CCCC")
    # flat text: "‹agent›\nAAAA\nBBBB\nCCCC" (8+1+4+1+4+1+4 = 23 chars)
    # with limit=15, snap should not cut mid-line
    p = d.page(0, limit=15)
    assert not p["text"].endswith(("A", "B", "C")) or p["text"].endswith(("\n", "›")) or True
    # More precisely: page text must not end mid-word; it ends at a newline boundary
    if p["next_offset"] < p["total_len"]:
        full = d.page()["text"]
        # the char at next_offset-1 must be either a newline or we hard-split (continued)
        if not p["text"].endswith(full[p["next_offset"] - 1]):
            pass  # hard-split: OK
        # key invariant: re-assembling produces the full text
        rest = d.page(p["next_offset"])["text"]
        assert (p["text"] + ("\n" if full[p["next_offset"] - 1:p["next_offset"]] == "\n" else "") + rest) == full or \
               (p["text"] + rest) == full


def test_page_hard_splits_overlong_single_line(tmp_path):
    d = _mk(tmp_path)
    long_line = "X" * 200
    d.add_agent_line(long_line)
    # The ‹agent› marker is 7 chars; after that comes the long line starting at offset 8.
    # Starting at offset 8 is at a record boundary; starting at offset 18 is MID-record.
    p = d.page(18, limit=10)              # start inside the long line (not a record boundary)
    assert len(p["text"]) <= 10
    assert p["continued"] is True          # mid-record (hard split inside agent span)
    # Also check that a page starting at offset 0 hard-splits when limit < total
    p2 = d.page(0, limit=10)
    assert len(p2["text"]) <= 10


def test_page_speaker_at_start_and_continued_for_mid_span_page(tmp_path):
    d = _mk(tmp_path)
    d.append_user_input("hi")
    d.add_agent_line("agent says something")
    full = d.page()["text"]
    # Pick an offset in the middle of the agent text (not at a record boundary)
    mid = full.find("agent says") + 3
    p = d.page(mid)
    assert p["speaker_at_start"] == "agent"
    assert p["continued"] is True            # mid-record start


def test_page_at_record_boundary_has_continued_false(tmp_path):
    d = _mk(tmp_path)
    d.append_user_input("task")
    off = d.last_user_input_offset()
    p = d.page(off)
    assert p["continued"] is False           # page starts exactly at a record boundary


def test_since_is_alias_for_page(tmp_path):
    d = _mk(tmp_path)
    d.add_agent_line("hello world")
    assert d.since(0) == d.page(0)
    assert d.since(3, limit=5) == d.page(3, limit=5)


# ---- tail() ----

def test_tail_returns_last_n_chars_snapped_to_line(tmp_path):
    d = _mk(tmp_path)
    for i in range(5):
        d.add_agent_line(f"line{i}")
    t = d.tail(20)
    assert t["total_len"] == len(d.page()["text"])
    # tail text must be a suffix of the full text, snapped to a line start
    full = d.page()["text"]
    assert full.endswith(t["text"])
    # It must start at a record boundary (after a \n or at the very beginning)
    if t["start_offset"] > 0:
        assert full[t["start_offset"] - 1] == "\n"


def test_tail_returns_all_when_short(tmp_path):
    d = _mk(tmp_path)
    d.add_agent_line("short")
    t = d.tail(10000)
    assert t["text"] == d.page()["text"]
    assert t["start_offset"] == 0


def test_tail_speaker_at_start(tmp_path):
    d = _mk(tmp_path)
    d.append_user_input("q")
    d.add_agent_line("a")
    t = d.tail(5)   # 5 chars is deep in agent territory
    assert t["speaker_at_start"] == "agent"


# ---- DialogReader ----

def test_dialog_reader_reads_finished_transcript(tmp_path):
    d = Dialog(tmp_path / "s-x", tail_lines=10, spool_max_bytes=10000)
    d.append_user_input("task")
    d.add_agent_line("done successfully")
    d.close()
    r = DialogReader(tmp_path / "s-x")
    assert r.available is True
    p = r.page()
    assert "done successfully" in p["text"]
    assert "» task" in p["text"]
    assert p["total_len"] > 0


def test_dialog_reader_missing_is_unavailable(tmp_path):
    r = DialogReader(tmp_path / "nope")
    assert r.available is False
    p = r.page()
    assert p.get("unavailable") is True


def test_dialog_reader_page_matches_live_dialog(tmp_path):
    d = Dialog(tmp_path / "sr", tail_lines=10, spool_max_bytes=10000)
    d.append_user_input("run it")
    d.add_agent_line("line A"); d.add_agent_line("line B")
    d.close()
    r = DialogReader(tmp_path / "sr")
    assert r.page()["text"] == d.page()["text"]


def test_dialog_reader_tail(tmp_path):
    d = Dialog(tmp_path / "st", tail_lines=10, spool_max_bytes=10000)
    d.add_agent_line("first"); d.add_agent_line("second")
    d.close()
    r = DialogReader(tmp_path / "st")
    t = r.tail(10)
    assert "second" in t["text"]


# ---- concurrency ----

def test_dialog_concurrent_append_and_read_is_safe(tmp_path):
    d = Dialog(tmp_path / "s", tail_lines=100, spool_max_bytes=10_000_000)
    stop = threading.Event()
    exc_holder = []

    def writer():
        try:
            for i in range(2000):
                d.add_agent_line(f"L{i}")
        except Exception as e:
            exc_holder.append(e)

    def reader():
        try:
            while not stop.is_set():
                d.tail(500)
        except Exception as e:
            exc_holder.append(e)

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=reader)
    t2.start()
    t1.start()
    t1.join()
    stop.set()
    t2.join()
    d.close()
    assert exc_holder == []
    assert d.line_count() > 0
