"""The start-idempotency ledger — router-owned, durable.

The router assigns a session id BEFORE forwarding `/start` (design §3). Two reasons, both
fatal otherwise:
  * a worker HOOK can fire immediately after spawn, before the generation's `/start`
    response reaches the router — if the router only learned the mapping from that response,
    it could not route the early `/hook/<sid>`;
  * a LOST start response makes the caller retry, and the retry would land on whatever
    generation is active NOW — spawning a SECOND worker for the same task.

So a reservation is durable and keyed by (owner, idempotency_key): a replay returns the
ORIGINAL operation rather than re-picking the active generation.
"""
import hashlib
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path

from nelix_contracts.errors import OWNER_MISMATCH, UNKNOWN_SESSION, NelixError
from nelix_contracts.ids import new_session_id, validate_orchestration_id, validate_owner_id

from .store import _atomic_write, _read_json


@dataclass(frozen=True)
class Reservation:
    session_id: str
    state: str                    # "starting" | "started" | "failed"
    generation_id: str | None
    replay: bool                  # True when this key had already been reserved


class StartLedger:
    def __init__(self, root, *, clock=time.time, mint=new_session_id):
        self._root = Path(root)
        self._clock = clock
        self._mint = mint

    def _key_path(self, owner_id: str, idempotency_key: str) -> Path:
        # Namespaced per owner so one owner cannot replay another's key. Hashed because a
        # caller-supplied key is arbitrary text and must never become a path.
        digest = hashlib.sha256(f"{owner_id}\x00{idempotency_key}".encode()).hexdigest()
        return self._root / "ledger" / "keys" / f"{digest}.json"

    def _index_path(self, idempotency_key: str) -> Path:
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        return self._root / "ledger" / "index" / f"{digest}.json"

    def _session_path(self, session_id: str) -> Path:
        return self._root / "ledger" / "sessions" / f"{session_id}.json"

    def _load(self, path: Path):
        try:
            return _read_json(path, "ledger")
        except FileNotFoundError:
            return None

    def reserve(self, *, idempotency_key: str, owner_id: str, orchestration_id: str) -> Reservation:
        validate_owner_id(owner_id)
        validate_orchestration_id(orchestration_id)

        # A key seen under ANY owner: if the owners differ, refuse rather than hand over.
        index = self._load(self._index_path(idempotency_key))
        if index is not None and index["owner_id"] != owner_id:
            raise NelixError(OWNER_MISMATCH,
                             "idempotency key belongs to another owner")

        existing = self._load(self._key_path(owner_id, idempotency_key))
        if existing is not None:
            entry = self._load(self._session_path(existing["session_id"]))
            return Reservation(session_id=entry["session_id"], state=entry["state"],
                               generation_id=entry["generation_id"], replay=True)

        session_id = self._mint()
        entry = {"session_id": session_id, "owner_id": owner_id,
                 "orchestration_id": orchestration_id, "idempotency_key": idempotency_key,
                 "state": "starting", "generation_id": None,
                 "created_at": float(self._clock())}
        _atomic_write(self._session_path(session_id), entry)
        _atomic_write(self._key_path(owner_id, idempotency_key), {"session_id": session_id})
        _atomic_write(self._index_path(idempotency_key), {"owner_id": owner_id})
        return Reservation(session_id=session_id, state="starting", generation_id=None,
                           replay=False)

    def _transition(self, session_id: str, **changes) -> None:
        entry = self._load(self._session_path(session_id))
        if entry is None:
            raise NelixError(UNKNOWN_SESSION, f"no reservation for {session_id}")
        entry.update(changes)
        _atomic_write(self._session_path(session_id), entry)

    def commit(self, session_id: str, generation_id: str) -> None:
        self._transition(session_id, state="started", generation_id=generation_id)

    def fail(self, session_id: str, reason: str) -> None:
        self._transition(session_id, state="failed", reason=reason)

    def lookup(self, idempotency_key: str) -> Reservation | None:
        index = self._load(self._index_path(idempotency_key))
        if index is None:
            return None
        existing = self._load(self._key_path(index["owner_id"], idempotency_key))
        entry = self._load(self._session_path(existing["session_id"]))
        return Reservation(session_id=entry["session_id"], state=entry["state"],
                           generation_id=entry["generation_id"], replay=True)
