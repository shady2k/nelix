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
from pathlib import Path

from nelix_contracts.errors import INVALID_REQUEST, UNKNOWN_SESSION, NelixError
from nelix_contracts.records import SessionRecord, assert_owner


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
