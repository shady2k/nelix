"""Durable per-session dialog: raw forensic spool + cleaned transcript line records.

Thread-safe: an internal RLock guards every public method, so the TranscriptBuilder can write
concurrently with RPC reads without external serialization.
"""
import json
import os
import threading
from pathlib import Path

import paths


class Dialog:
    def __init__(self, dir_path, tail_lines, spool_max_bytes):
        self._dir = Path(dir_path)
        paths.ensure_private_dir(self._dir)         # 0700 session dir (holds secret-bearing output)
        self._raw_path = self._dir / "raw"
        self._jsonl_path = self._dir / "transcript.jsonl"
        self._tail_lines = int(tail_lines)
        self._spool_max = int(spool_max_bytes)
        self._raw = open(self._raw_path, "ab", opener=paths.private_opener)      # 0600 raw spool
        self._jsonl = open(self._jsonl_path, "a", opener=paths.private_opener)   # 0600 transcript
        self._raw_len = self._raw_path.stat().st_size if self._raw_path.exists() else 0
        self._lines = []          # in-memory full index for the session lifetime: list[(turn, text)]
        self._turn = 0
        self._turn_starts = [0]   # turn i starts at line index turn_starts[i]
        self._lock = threading.RLock()
        self._raw_total = self._raw_len             # absolute bytes ever appended (monotonic)
        self._raw_base = 0                          # absolute offset of the first retained raw byte
        self._offsets_path = self._dir / "turn_offsets.json"
        self._turn_offsets = [0]                    # absolute raw offset where each turn starts
        self._write_offsets()

    # ---- writes ----
    def append_raw(self, chunk):
        with self._lock:
            self._raw.write(chunk)
            self._raw.flush()
            self._raw_len += len(chunk)
            self._raw_total += len(chunk)
            if self._raw_len > self._spool_max:
                self._raw.close()
                data = self._raw_path.read_bytes()[-self._spool_max:]
                self._raw_path.write_bytes(data)
                self._raw_len = len(data)
                self._raw_base = self._raw_total - self._raw_len
                self._raw = open(self._raw_path, "ab", opener=paths.private_opener)

    def add_line(self, text):
        with self._lock:
            idx = len(self._lines)
            self._lines.append((self._turn, text))
            self._jsonl.write(json.dumps({"idx": idx, "turn": self._turn, "text": text}) + "\n")
            self._jsonl.flush()
            return idx

    def mark_turn_boundary(self):
        with self._lock:
            self._turn += 1
            self._turn_starts.append(len(self._lines))
            self._turn_offsets.append(self._raw_total)
            self._write_offsets()
            return self._turn

    # ---- reads ----
    def turn_count(self):
        with self._lock:
            return self._turn + 1

    def line_count(self):
        with self._lock:
            return len(self._lines)

    def current_turn(self):
        with self._lock:
            return self._turn

    def raw_base_offset(self):
        with self._lock:
            return self._raw_base

    def current_turn_page(self, limit=None):
        with self._lock:
            turn = self._turn
            start = self._turn_starts[turn]
            end = len(self._lines)
            page = self._page(self._slice(start, end), 0, limit)
            return {"turn": turn, "start": start, "end": end, **page}

    def _slice(self, start, end):
        return "\n".join(t for (_turn, t) in self._lines[start:end])

    def _page(self, text, offset, limit):
        total = len(text)
        if offset:
            text = text[offset:]
        truncated = False
        if limit is not None and len(text) > limit:
            text = text[:limit]; truncated = True
        return {"text": text, "offset": offset, "total_len": total, "truncated": truncated,
                "unavailable": False}

    def _write_offsets(self):
        # best-effort, atomic (temp + rename), 0600 — never fail a session over the replay sidecar.
        tmp = self._offsets_path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", opener=paths.private_opener) as f:
                json.dump({"raw_base_offset": self._raw_base, "turns": self._turn_offsets}, f)
            os.replace(tmp, self._offsets_path)
        except Exception:
            pass

    def turn_text(self, turn_index, offset=0, limit=None):
        with self._lock:
            if turn_index < 0 or turn_index >= self._turn + 1:
                return {"text": "", "offset": offset, "total_len": 0, "truncated": False,
                        "unavailable": True}
            start = self._turn_starts[turn_index]
            end = self._turn_starts[turn_index + 1] if turn_index + 1 < len(self._turn_starts) \
                else len(self._lines)
            return self._page(self._slice(start, end), offset, limit)

    def range_text(self, start_idx, end_idx, offset=0, limit=None):
        with self._lock:
            return self._page(self._slice(start_idx, end_idx), offset, limit)

    def tail_text(self, max_chars):
        with self._lock:
            text = self._slice(max(0, len(self._lines) - self._tail_lines), len(self._lines))
            return text[-max_chars:] if max_chars and len(text) > max_chars else text

    def close(self):
        with self._lock:
            for f in (self._raw, self._jsonl):
                try:
                    f.close()
                except Exception:
                    pass


class DialogReader:
    """Read-only transcript paging from disk by session dir, for a session no longer live
    in the manager. Reconstructs turns from transcript.jsonl ({idx, turn, text} records)."""

    def __init__(self, session_dir):
        self._lines = []           # list[(turn, text)]
        self._turn_starts = [0]
        path = Path(session_dir) / "transcript.jsonl"
        try:
            with open(path) as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    rec = json.loads(raw)
                    turn, text = rec.get("turn", 0), rec.get("text", "")
                    while len(self._turn_starts) <= turn:
                        self._turn_starts.append(len(self._lines))
                    self._lines.append((turn, text))
            self._available = True
        except (OSError, ValueError):
            self._available = False
        self._max_turn = self._lines[-1][0] if self._lines else -1

    def turn_count(self):
        return self._max_turn + 1 if self._available else 0

    def _slice(self, start, end):
        return "\n".join(t for (_t, t) in self._lines[start:end])

    def turn_text(self, turn_index, offset=0, limit=None):
        if not self._available or turn_index < 0 or turn_index > self._max_turn:
            return {"text": "", "offset": offset, "total_len": 0, "truncated": False,
                    "unavailable": True}
        start = self._turn_starts[turn_index]
        end = self._turn_starts[turn_index + 1] if turn_index + 1 < len(self._turn_starts) \
            else len(self._lines)
        text = self._slice(start, end)
        total = len(text)
        if offset:
            text = text[offset:]
        truncated = False
        if limit is not None and len(text) > limit:
            text = text[:limit]; truncated = True
        return {"text": text, "offset": offset, "total_len": total, "truncated": truncated,
                "unavailable": False}
