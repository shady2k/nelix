"""Generation-neutral durable state under NELIX_HOME.

"Generation-neutral" is the point (design §5): ANY generation may write a record and the
ACTIVE generation serves archived reads, so a retiring generation's results do not vanish
with it. All writes are atomic (temp + os.replace) because an owner record is an ACCESS
invariant — a half-written file must never be readable.

The clock is injectable: tests freeze it rather than sleep (the nelix-3s3 pattern).
"""
import json
import os
import time
from dataclasses import replace
from pathlib import Path

from nelix_contracts.errors import INVALID_REQUEST, UNKNOWN_SESSION, NelixError
from nelix_contracts.records import SessionRecord, TerminalRecord, assert_owner


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)          # atomic within a filesystem


def _read_json(path: Path, what: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as e:
        # Fail closed. A malformed record must never be coerced into a default — it carries
        # the owner, and defaulting an owner would hand a session to the wrong harness.
        raise NelixError(INVALID_REQUEST, f"corrupt {what} record: {e}") from None


class Store:
    def __init__(self, root, *, clock=time.time):
        self._root = Path(root)
        self._clock = clock

    # ---- sessions -------------------------------------------------------------
    def _session_path(self, session_id: str) -> Path:
        return self._root / "sessions" / f"{session_id}.json"

    def put_session(self, record: SessionRecord) -> None:
        _atomic_write(self._session_path(record.session_id), record.to_dict())

    def get_session(self, session_id: str, *, owner_id: str) -> SessionRecord:
        try:
            raw = _read_json(self._session_path(session_id), "session")
        except FileNotFoundError:
            raise NelixError(UNKNOWN_SESSION, f"no such session: {session_id}") from None
        record = SessionRecord.from_dict(raw)
        assert_owner(record, owner_id)
        return record

    def list_sessions(self, owner_id: str) -> list:
        out = []
        for path in sorted((self._root / "sessions").glob("s-*.json")):
            record = SessionRecord.from_dict(_read_json(path, "session"))
            if record.owner_id == owner_id:
                out.append(record)
        return out

    # ---- terminal records -----------------------------------------------------
    # These OUTLIVE their generation. A retiring generation's in-memory ring and inventory
    # vanish with it, so the record must already be here before the live session is removed
    # (design §5's ordering invariant: publish -> persist -> board-visible -> remove live).
    def _terminal_path(self, session_id: str) -> Path:
        return self._root / "terminal" / f"{session_id}.json"

    def put_terminal(self, record: TerminalRecord) -> None:
        _atomic_write(self._terminal_path(record.session_id), record.to_dict())

    def get_terminal(self, session_id: str, *, owner_id: str) -> TerminalRecord:
        try:
            raw = _read_json(self._terminal_path(session_id), "terminal")
        except FileNotFoundError:
            raise NelixError(UNKNOWN_SESSION, f"no terminal record: {session_id}") from None
        record = TerminalRecord.from_dict(raw)
        assert_owner(record, owner_id)
        return record

    def list_terminal(self, owner_id: str) -> list:
        out = []
        for path in sorted((self._root / "terminal").glob("s-*.json")):
            record = TerminalRecord.from_dict(_read_json(path, "terminal"))
            if record.owner_id == owner_id:
                out.append(record)
        return out

    def ack_terminal(self, session_id: str, *, owner_id: str) -> TerminalRecord:
        """Idempotent: a repeated ack (e.g. after a lost reply) returns the SAME record with
        its ORIGINAL timestamp, never re-stamped and never an error."""
        record = self.get_terminal(session_id, owner_id=owner_id)
        if record.acknowledged_at is not None:
            return record
        acked = replace(record, acknowledged_at=float(self._clock()))
        _atomic_write(self._terminal_path(session_id), acked.to_dict())
        return acked

    def prune_terminal(self, *, max_age_seconds: float, max_count: int) -> int:
        """Drop acknowledged records, and bound the rest by age and count so an owner that
        never returns cannot grow storage forever. Returns how many were removed."""
        entries = []
        for path in (self._root / "terminal").glob("s-*.json"):
            entries.append((TerminalRecord.from_dict(_read_json(path, "terminal")), path))
        now = float(self._clock())
        doomed = [(r, p) for r, p in entries
                  if r.acknowledged_at is not None or (now - r.ended_at) > max_age_seconds]
        survivors = [(r, p) for r, p in entries if (r, p) not in doomed]
        survivors.sort(key=lambda rp: rp[0].ended_at)                 # oldest first
        overflow = max(0, len(survivors) - max_count)
        doomed.extend(survivors[:overflow])
        for _record, path in doomed:
            path.unlink(missing_ok=True)
        return len(doomed)
