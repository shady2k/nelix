"""Durable per-session dialog: raw forensic spool + cleaned transcript line records.

No internal locking — the owning Session serializes access (see session.py). turn/range reads
return paginated cleaned text; raw is never paginated to Hermes.
"""
import json
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

    # ---- writes ----
    def append_raw(self, chunk):
        self._raw.write(chunk)
        self._raw.flush()
        self._raw_len += len(chunk)
        if self._raw_len > self._spool_max:
            self._raw.close()
            data = self._raw_path.read_bytes()[-self._spool_max:]
            self._raw_path.write_bytes(data)
            self._raw_len = len(data)
            self._raw = open(self._raw_path, "ab", opener=paths.private_opener)

    def add_line(self, text):
        idx = len(self._lines)
        self._lines.append((self._turn, text))
        self._jsonl.write(json.dumps({"idx": idx, "turn": self._turn, "text": text}) + "\n")
        self._jsonl.flush()
        return idx

    def mark_turn_boundary(self):
        self._turn += 1
        self._turn_starts.append(len(self._lines))
        return self._turn

    # ---- reads ----
    def turn_count(self):
        return self._turn + 1

    def line_count(self):
        return len(self._lines)

    def current_turn(self):
        return self._turn

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

    def turn_text(self, turn_index, offset=0, limit=None):
        if turn_index < 0 or turn_index >= self.turn_count():
            return {"text": "", "offset": offset, "total_len": 0, "truncated": False,
                    "unavailable": True}
        start = self._turn_starts[turn_index]
        end = self._turn_starts[turn_index + 1] if turn_index + 1 < len(self._turn_starts) \
            else len(self._lines)
        return self._page(self._slice(start, end), offset, limit)

    def range_text(self, start_idx, end_idx, offset=0, limit=None):
        return self._page(self._slice(start_idx, end_idx), offset, limit)

    def tail_text(self, max_chars):
        text = self._slice(max(0, len(self._lines) - self._tail_lines), len(self._lines))
        return text[-max_chars:] if max_chars and len(text) > max_chars else text

    def close(self):
        for f in (self._raw, self._jsonl):
            try:
                f.close()
            except Exception:
                pass
