"""The start-idempotency ledger — router-owned, durable, transactional.

The router assigns a session id BEFORE forwarding `/start` (design §3). Two reasons, both
fatal otherwise:
  * a worker HOOK can fire immediately after spawn, before the generation's `/start`
    response reaches the router — if the router only learned the mapping from that response,
    it could not route the early `/hook/<sid>`;
  * a LOST start response makes the caller retry, and the retry would land on whatever
    generation is active NOW — spawning a SECOND worker for the same task.

Three properties this must have, and rev 1 had none of them:
  * RESERVE IS ATOMIC. `UNIQUE (owner_id, idempotency_key)` makes the database the arbiter;
    a check-then-write in application code can interleave and mint two ids for one key.
  * THE GENERATION IS PERSISTED BEFORE FORWARDING (`assign_generation`). Otherwise a retry
    after a lost response finds `generation_id=None` and cannot recover the original
    operation — the exact ambiguity the ledger exists to close.
  * IDEMPOTENCY COMPARES THE REQUEST, not just the key. Same key + same request = replay
    (success). Same key + different request = `idempotency_conflict`, never a silent return
    of someone else's task.

Keys are namespaced PER OWNER: (hermes:local, "deploy") and (claude-code:1, "deploy") are
independent operations. There is deliberately no global key index — rev 1 had one, and it
both contradicted the namespacing and raced.
"""
import sqlite3
import time
from dataclasses import dataclass

from nelix_contracts.errors import (
    IDEMPOTENCY_CONFLICT, INVALID_REQUEST, STORE_CORRUPT, UNKNOWN_SESSION, NelixError,
)
from nelix_contracts.ids import (
    InvalidId, new_session_id, validate_generation_id, validate_orchestration_id,
    validate_owner_id, validate_session_id,
)

from .db import connect, translates_sqlite

_COLS = ("session_id, owner_id, orchestration_id, idempotency_key, request_fingerprint, "
         "state, generation_id, reason, created_at")


@dataclass(frozen=True)
class Reservation:
    session_id: str
    state: str                    # "starting" | "started" | "failed"
    generation_id: str | None
    reason: str | None
    replay: bool                  # True when this (owner, key) had already been reserved


def _row_to_reservation(row, *, replay: bool) -> Reservation:
    """Validate on the way OUT of storage. rev 6 copied these fields straight from SQLite, so
    a malformed generation id or an impossible state reached the router looking valid — and
    the router routes on exactly these."""
    try:
        validate_session_id(row["session_id"])
        if row["generation_id"] is not None:
            validate_generation_id(row["generation_id"])
    except InvalidId as e:
        raise NelixError(STORE_CORRUPT, f"stored reservation is unreadable: {e}") from None
    if row["state"] not in ("starting", "started", "failed"):
        raise NelixError(STORE_CORRUPT,
                         f"stored reservation has an impossible state: {row['state']!r}")
    return Reservation(session_id=row["session_id"], state=row["state"],
                       generation_id=row["generation_id"], reason=row["reason"],
                       replay=replay)


class StartLedger:
    def __init__(self, root, *, clock=time.time, mint=new_session_id):
        self._conn = connect(root)
        self._clock = clock
        self._mint = mint

    @translates_sqlite
    def close(self):
        self._conn.close()

    @translates_sqlite
    def reserve(self, *, idempotency_key, owner_id, orchestration_id,
                request_fingerprint) -> Reservation:
        try:
            validate_owner_id(owner_id)
            validate_orchestration_id(orchestration_id)
        except InvalidId as e:
            raise NelixError(INVALID_REQUEST, str(e)) from None
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise NelixError(INVALID_REQUEST,
                             f"idempotency_key must be a non-empty string: "
                             f"{idempotency_key!r}")
        if not isinstance(request_fingerprint, str) or not request_fingerprint:
            raise NelixError(INVALID_REQUEST, "request_fingerprint must be a non-empty string")

        session_id = self._mint()
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    f"INSERT INTO starts ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    (session_id, owner_id, orchestration_id, idempotency_key,
                     request_fingerprint, "starting", None, None, float(self._clock())))
                return Reservation(session_id=session_id, state="starting",
                                   generation_id=None, reason=None, replay=False)
            except sqlite3.IntegrityError:
                pass
            # The UNIQUE constraint fired: this (owner, key) is already reserved. Return the
            # ORIGINAL operation — never re-pick the active generation.
            row = self._conn.execute(
                f"SELECT {_COLS} FROM starts WHERE owner_id=? AND idempotency_key=?",
                (owner_id, idempotency_key)).fetchone()
            if row is None:
                # The IntegrityError was NOT the owner/key constraint — the only other
                # candidate is a session_id PK collision, which uuid4 makes astronomically
                # unlikely and which we must not paper over.
                raise NelixError(STORE_CORRUPT,
                                 "reservation insert failed on an unexpected constraint")
            if (row["request_fingerprint"] != request_fingerprint
                    or row["orchestration_id"] != orchestration_id):
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 "idempotency key was used for a different request")
            return _row_to_reservation(row, replay=True)

    def _require(self, session_id):
        row = self._conn.execute(
            f"SELECT {_COLS} FROM starts WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise NelixError(UNKNOWN_SESSION, f"no reservation for {session_id}")
        return row

    @translates_sqlite
    def assign_generation(self, session_id: str, generation_id: str) -> None:
        """Record the chosen generation BEFORE the request reaches it. Idempotent for the
        same generation; a different one while still starting is a conflict."""
        try:
            validate_generation_id(generation_id)
        except InvalidId as e:
            raise NelixError(INVALID_REQUEST, str(e)) from None
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._require(session_id)
            if row["state"] != "starting":
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 f"cannot assign a generation in state {row['state']}")
            if row["generation_id"] not in (None, generation_id):
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 "reservation is already assigned to another generation")
            self._conn.execute("UPDATE starts SET generation_id=? WHERE session_id=?",
                               (generation_id, session_id))

    @translates_sqlite
    def commit(self, session_id: str, generation_id: str) -> None:
        """Mark the start succeeded — on the generation it was ASSIGNED to, and no other.

        `commit` never writes generation_id. Only `assign_generation` binds it, and it does
        so BEFORE the request is forwarded; a commit that could rebind would reopen the exact
        lost-response ambiguity the assignment exists to close.
        """
        try:
            validate_generation_id(generation_id)
        except InvalidId as e:
            raise NelixError(INVALID_REQUEST, str(e)) from None
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._require(session_id)
            if row["state"] == "failed":
                raise NelixError(IDEMPOTENCY_CONFLICT, "cannot commit a failed start")
            # ONE guard, not two. `generation_id is None` was a separate check, but deleting
            # it changed nothing — the comparison below already fires (None != anything) with
            # the same code, so it could never be individually detected. A branch whose
            # deletion cannot change behaviour is not a guard; it is a diagnostic. Keep the
            # diagnostic, drop the pretence.
            if row["generation_id"] != generation_id:
                if row["generation_id"] is None:
                    raise NelixError(IDEMPOTENCY_CONFLICT,
                                     "cannot commit a start that was never assigned a "
                                     "generation")
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 "start was assigned to a different generation")
            # state only — the binding is assign_generation's alone.
            self._conn.execute("UPDATE starts SET state='started' WHERE session_id=?",
                               (session_id,))

    @translates_sqlite
    def fail(self, session_id: str, reason: str) -> None:
        """Record a failed start. Idempotent for the same reason; a DIFFERENT reason is a
        conflict — a durable failure result must not be rewritten under a replay."""
        if not isinstance(reason, str) or not reason:
            raise NelixError(INVALID_REQUEST, f"reason must be a non-empty string: {reason!r}")
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._require(session_id)
            if row["state"] == "started":
                raise NelixError(IDEMPOTENCY_CONFLICT, "cannot fail an already-started start")
            if row["state"] == "failed":
                if row["reason"] == reason:
                    return
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 "start already failed for a different reason")
            # The MIRROR of create_session's failed-start guard. Both orders must be safe:
            # fail-then-create is refused there; create-then-fail is refused here. The router
            # cannot arbitrate — it calls fail() on a forward timeout without knowing whether
            # the generation's create_session committed a moment before. Measured: 44/200
            # races left a live session AND a failed start, and a retry then reports a durable
            # failure while a worker is running.
            if self._conn.execute("SELECT 1 FROM sessions WHERE session_id=?",
                                  (session_id,)).fetchone() is not None:
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 f"start {session_id} already acquired a session; it cannot "
                                 f"be failed")
            self._conn.execute(
                "UPDATE starts SET state='failed', reason=? WHERE session_id=?",
                (reason, session_id))

    @translates_sqlite
    def lookup(self, idempotency_key: str, *, owner_id: str) -> "Reservation | None":
        """Owner-guarded: rev 1's lookup took no owner at all, so it handed any caller
        another owner's session_id, state and generation."""
        row = self._conn.execute(
            f"SELECT {_COLS} FROM starts WHERE owner_id=? AND idempotency_key=?",
            (owner_id, idempotency_key)).fetchone()
        return None if row is None else _row_to_reservation(row, replay=True)
