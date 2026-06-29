"""Durable per-session dialog: raw forensic spool + flat-log transcript records.

Thread-safe: an internal RLock guards every public method, so the TranscriptBuilder can write
concurrently with RPC reads without external serialization.

Records are ``{idx, kind, speaker, text}`` (``kind`` ∈ ``"line"`` | ``"marker"``;
``speaker`` ∈ ``"agent"`` | ``"user"``).  The flat text is
``"\\n".join(r["text"] for r in records)``.  Pagination is over char offsets in that text.
"""
import json
import threading
from pathlib import Path

import paths


class Dialog:
    def __init__(self, dir_path, tail_lines, spool_max_bytes):
        self._dir = Path(dir_path)
        paths.ensure_private_dir(self._dir)         # 0700 session dir (holds secret-bearing output)
        self._raw_path = self._dir / "raw"
        self._jsonl_path = self._dir / "transcript.jsonl"
        self._spool_max = int(spool_max_bytes)
        self._raw = open(self._raw_path, "ab", opener=paths.private_opener)      # 0600 raw spool
        self._jsonl = open(self._jsonl_path, "a", opener=paths.private_opener)   # 0600 transcript
        self._raw_len = self._raw_path.stat().st_size if self._raw_path.exists() else 0
        self._records = []               # list[dict] — {idx, kind, speaker, text}
        self._flat_len = 0               # char length of "\n".join(r["text"] for r in _records)
        self._current_speaker = None     # "agent" | "user" | None (no content yet)
        self._last_user_input_offset = 0
        self._last_agent_marker_offset = 0
        self._lock = threading.RLock()
        self._raw_total = self._raw_len             # absolute bytes ever appended (monotonic)
        self._raw_base = 0                          # absolute offset of the first retained raw byte

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

    def _append_record(self, kind, speaker, text):
        """Append one record; return its start char-offset in the flat text.

        Defensive: if *text* contains ``\\n``, splits into multiple single-line records
        (first keeps *kind*, continuation records use ``'line'``).
        """
        lines = text.split("\n") if "\n" in text else [text]
        first_offset = None
        for i, line in enumerate(lines):
            rec_kind = kind if i == 0 else "line"
            idx = len(self._records)
            start_offset = (self._flat_len + 1) if self._records else 0
            if first_offset is None:
                first_offset = start_offset
            self._flat_len = start_offset + len(line)
            rec = {"idx": idx, "kind": rec_kind, "speaker": speaker, "text": line}
            self._records.append(rec)
            self._jsonl.write(json.dumps(rec) + "\n")
        self._jsonl.flush()
        return first_offset

    def append_user_input(self, text):
        """Append a user-input marker; return its start offset.

        Multiline *text* is split on ``\\n``: the first line becomes a ``marker`` record with
        the ``» `` prefix; each subsequent line becomes a ``line`` record (same speaker, no
        prefix).  This preserves the single-line-per-record invariant.
        """
        with self._lock:
            lines = text.split("\n")
            offset = self._append_record("marker", "user", "» " + lines[0])
            for line in lines[1:]:
                self._append_record("line", "user", line)
            self._current_speaker = "user"
            self._last_user_input_offset = offset
            return offset

    def add_agent_line(self, text):
        """Append one agent content line.  Emits a transition marker when not already in an
        agent span (once per span, not per line)."""
        with self._lock:
            if self._current_speaker != "agent":
                marker_offset = self._append_record("marker", "agent", "‹agent›")
                self._current_speaker = "agent"
                self._last_agent_marker_offset = marker_offset
            self._append_record("line", "agent", text)

    # ---- reads ----
    def _flat_text(self):
        return "\n".join(r["text"] for r in self._records)

    @staticmethod
    def _speaker_at(text, records, offset):
        """Speaker of the record that contains ``offset``."""
        if not records:
            return "agent"
        pos = 0
        speaker = records[0]["speaker"]
        for rec in records:
            if pos > offset:
                break
            speaker = rec["speaker"]
            pos += len(rec["text"]) + 1
        return speaker

    @staticmethod
    def _is_at_record_boundary(text, offset):
        """True when ``offset`` is exactly at the start of a record."""
        if offset == 0:
            return True
        if 0 < offset <= len(text):
            return text[offset - 1] == "\n"
        return False

    @staticmethod
    def _snap_page(chunk, offset, limit):
        """Return ``(page_text, next_offset)`` for ``chunk = flat_text[offset:]``.

        Snapping rules (from spec):
        - Find the last ``\\n`` in ``chunk[:limit]``.
        - If none, or if it would leave < 60 % of the limit, hard-split at ``limit``
          (caller detects mid-record via ``_is_at_record_boundary``).
        - Otherwise snap to the ``\\n`` (``next_offset`` skips the separator).
        """
        cut = chunk[:limit].rfind("\n")
        if cut == -1 or cut < int(0.6 * limit):
            # No usable snap point — hard-split
            return chunk[:limit], offset + limit
        # Snap: INCLUDE the boundary newline in the page so that
        # flat_text[start_offset:next_offset] == page_text exactly and
        # "".join(pages) reproduces the full flat text without gaps.
        return chunk[:cut + 1], offset + cut + 1

    def page(self, offset=0, limit=None, snap=True):
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got {offset!r}")
        if limit is not None and limit <= 0:
            raise ValueError(f"limit must be > 0 (or None for 'read to end'), got {limit!r}")
        with self._lock:
            text = self._flat_text()
            total_len = len(text)
            speaker_at_start = self._speaker_at(text, self._records, offset)
            continued = not self._is_at_record_boundary(text, offset)

            if offset >= total_len:
                return {"text": "", "start_offset": offset, "next_offset": total_len,
                        "speaker_at_start": speaker_at_start, "continued": continued,
                        "total_len": total_len}

            chunk = text[offset:]

            if limit is None or len(chunk) <= limit:
                return {"text": chunk, "start_offset": offset, "next_offset": total_len,
                        "speaker_at_start": speaker_at_start, "continued": continued,
                        "total_len": total_len}

            if snap:
                page_text, next_offset = self._snap_page(chunk, offset, limit)
            else:
                page_text, next_offset = chunk[:limit], offset + limit

            return {"text": page_text, "start_offset": offset, "next_offset": next_offset,
                    "speaker_at_start": speaker_at_start, "continued": continued,
                    "total_len": total_len}

    def tail(self, max_chars):
        """Return the last ``max_chars`` of the flat text, snapped to a line start."""
        with self._lock:
            text = self._flat_text()
            total_len = len(text)
            if max_chars is None or total_len <= max_chars:
                start_offset = 0
                excerpt = text
            else:
                start_raw = total_len - max_chars
                nl = text.find("\n", start_raw)
                start_offset = (nl + 1) if nl != -1 else start_raw
                excerpt = text[start_offset:]
            speaker = self._speaker_at(text, self._records, start_offset)
            return {"text": excerpt, "speaker_at_start": speaker,
                    "start_offset": start_offset, "total_len": total_len}

    def since(self, offset, limit=None):
        """Convenience alias for ``page(offset, limit)`` — reads from an anchor offset."""
        return self.page(offset, limit)

    def last_user_input_offset(self):
        with self._lock:
            return self._last_user_input_offset

    def last_agent_marker_offset(self):
        with self._lock:
            return self._last_agent_marker_offset

    def line_count(self):
        with self._lock:
            return len(self._records)

    def raw_base_offset(self):
        with self._lock:
            return self._raw_base

    def close(self):
        with self._lock:
            for f in (self._raw, self._jsonl):
                try:
                    f.close()
                except Exception:
                    pass


class DialogReader:
    """Read-only transcript paging from disk for a session no longer live in the manager.
    Reconstructs the flat log from ``transcript.jsonl`` (``{idx, kind, speaker, text}``
    records; also accepts the old ``{idx, turn, text}`` format for backward compat)."""

    def __init__(self, session_dir):
        self._records = []
        self._flat_len = 0
        path = Path(session_dir) / "transcript.jsonl"
        try:
            with open(path) as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    rec = json.loads(raw)
                    text = rec.get("text", "")
                    kind = rec.get("kind", "line")
                    speaker = rec.get("speaker", "agent")
                    start_offset = (self._flat_len + 1) if self._records else 0
                    self._flat_len = start_offset + len(text)
                    self._records.append({"idx": rec.get("idx", len(self._records)),
                                         "kind": kind, "speaker": speaker, "text": text})
            self._available = True
        except (OSError, ValueError):
            self._available = False

    @property
    def available(self):
        return self._available

    def _flat_text(self):
        return "\n".join(r["text"] for r in self._records)

    def page(self, offset=0, limit=None, snap=True):
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got {offset!r}")
        if limit is not None and limit <= 0:
            raise ValueError(f"limit must be > 0 (or None for 'read to end'), got {limit!r}")
        if not self._available:
            return {"text": "", "start_offset": offset, "next_offset": 0,
                    "speaker_at_start": "agent", "continued": False,
                    "total_len": 0, "unavailable": True}
        text = self._flat_text()
        total_len = len(text)
        speaker_at_start = Dialog._speaker_at(text, self._records, offset)
        continued = not Dialog._is_at_record_boundary(text, offset)

        if offset >= total_len:
            return {"text": "", "start_offset": offset, "next_offset": total_len,
                    "speaker_at_start": speaker_at_start, "continued": continued,
                    "total_len": total_len, "unavailable": False}

        chunk = text[offset:]

        if limit is None or len(chunk) <= limit:
            return {"text": chunk, "start_offset": offset, "next_offset": total_len,
                    "speaker_at_start": speaker_at_start, "continued": continued,
                    "total_len": total_len, "unavailable": False}

        if snap:
            page_text, next_offset = Dialog._snap_page(chunk, offset, limit)
        else:
            page_text, next_offset = chunk[:limit], offset + limit

        return {"text": page_text, "start_offset": offset, "next_offset": next_offset,
                "speaker_at_start": speaker_at_start, "continued": continued,
                "total_len": total_len, "unavailable": False}

    def tail(self, max_chars):
        if not self._available:
            return {"text": "", "speaker_at_start": "agent", "start_offset": 0, "total_len": 0}
        text = self._flat_text()
        total_len = len(text)
        if max_chars is None or total_len <= max_chars:
            start_offset = 0
            excerpt = text
        else:
            start_raw = total_len - max_chars
            nl = text.find("\n", start_raw)
            start_offset = (nl + 1) if nl != -1 else start_raw
            excerpt = text[start_offset:]
        speaker = Dialog._speaker_at(text, self._records, start_offset)
        return {"text": excerpt, "speaker_at_start": speaker,
                "start_offset": start_offset, "total_len": total_len}

    def since(self, offset, limit=None):
        return self.page(offset, limit)
