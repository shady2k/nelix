"""Generation-neutral durable state under NELIX_HOME, on SQLite.

"Generation-neutral" is the point (design §5): ANY generation may write a record and the
ACTIVE generation serves archived reads, so a retiring generation's results do not vanish
with it.

Two rules that look similar and are not:
  * `get_*` FAILS CLOSED on a record it cannot read — the caller asked for that record.
  * `list_*` SKIPS any row it cannot read — future schema, corrupt affinity, anything. One
    unreadable row must never blind an owner to their own board.

The clock is injectable: tests freeze it rather than sleep (the nelix-3s3 pattern).
"""
import math
import sqlite3
import time

from nelix_contracts.errors import (
    DUPLICATE_START, IDEMPOTENCY_CONFLICT, INVALID_REQUEST, OWNER_MISMATCH, SCHEMA_TOO_NEW,
    STORE_CORRUPT, UNKNOWN_SESSION, NelixError,
)
from nelix_contracts.records import (
    SCHEMA_VERSION, SessionRecord, TerminalRecord, timestamp,
)

from .db import connect, translates_sqlite

# Identity is JOINED from starts, never stored in these tables (nelix-555). Three
# independent copies could disagree; one row cannot disagree with itself.
_SESSION_SELECT = (
    "SELECT s.session_id, st.owner_id, st.orchestration_id, st.generation_id, s.state, "
    "s.executor, s.task, s.cwd, s.model, s.created_at, s.schema_version "
    "FROM sessions s JOIN starts st ON st.session_id = s.session_id")
# f1k-rev5: this used to also `JOIN sessions s ON s.session_id = t.session_id`. It selected
# no column and could not filter anything: every terminal row is created by put_terminal only
# after joining sessions+starts itself (UNKNOWN_SESSION otherwise), and nothing in this
# package ever deletes a sessions row — so under this package's own writers, terminal implies
# sessions implies starts, unconditionally. Deleted rather than kept as unexercised defence
# against a hypothetical writer that opens the file without going through db.connect()'s
# `PRAGMA foreign_keys=ON` connection.
_TERMINAL_SELECT = (
    "SELECT t.session_id, st.owner_id, st.orchestration_id, st.generation_id, "
    "t.terminal_kind, t.summary, t.ended_at, t.acknowledged_at, t.schema_version "
    "FROM terminal t JOIN starts st ON st.session_id = t.session_id")

# transition_session's CAS UPDATE has no owner_id column to filter on directly (identity
# lives in starts, not sessions) — expressed as a subquery so the owner check stays part of
# the SAME atomic UPDATE rather than a separate check-then-write.
_OWNS_SESSION = "session_id IN (SELECT session_id FROM starts WHERE owner_id=?)"


def _read_rows(rows, record_type):
    """Deserialise what we can, SKIP what we cannot.

    The contract (design §5 / this module's docstring) is that one unreadable row must never
    blind an owner to their own board. rev 2 filtered only on schema_version, but SQLite has
    AFFINITY rather than types, so a row can be the current schema and still be garbage —
    and then the raise escaped the whole call, which is the very failure the filter existed
    to prevent. Skip on ANY per-row failure.
    """
    out, skipped = [], 0
    for row in rows:
        try:
            out.append(record_type.from_dict(dict(row)))
        except NelixError:
            skipped += 1     # future schema, corrupt affinity, anything: not our caller's problem
    return out, skipped


def _decode_stored(record_type, row):
    """Decode a row read from DURABLE STORAGE.

    Same decoder, different party at fault: `from_dict`'s INVALID_REQUEST means "the caller
    handed me nonsense", which is the right answer for a contract boundary — and the wrong one
    here, because this row came off our own disk. A caller told "your request is invalid" goes
    and fixes their request; the damage is ours.

    SCHEMA_TOO_NEW passes through untouched: "written by a newer build" is a distinct,
    actionable condition, not damage.
    """
    try:
        return record_type.from_dict(dict(row))
    except NelixError as e:
        if e.code == SCHEMA_TOO_NEW:
            raise
        raise NelixError(STORE_CORRUPT, f"stored record is unreadable: {e.message}") from None


class Store:
    def __init__(self, root, *, clock=time.time, timeout: float = 30.0):
        self._conn = connect(root, timeout=timeout)
        self._clock = clock

    @translates_sqlite
    def close(self):
        self._conn.close()

    # ---- sessions -------------------------------------------------------------
    @translates_sqlite
    def create_session(self, session_id: str, *, state: str, executor: str, task: str,
                       cwd: str, model, created_at: float) -> None:
        """Create the runtime row for an assigned, NOT-failed start.

        Takes no owner/orchestration/generation: they are read from the start row, so a
        session physically cannot disagree with the reservation that created it.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            start = self._conn.execute(
                "SELECT owner_id, orchestration_id, generation_id, state "
                "FROM starts WHERE session_id=?", (session_id,)).fetchone()
            if start is None:
                raise NelixError(UNKNOWN_SESSION, f"no start for session {session_id}")
            if start["generation_id"] is None:
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 f"start {session_id} has no assigned generation yet")
            # The router calls fail() when a forward times out — exactly when the generation
            # may have created the session anyway. Accepting the session here would let the
            # caller retry the key, be told "failed", and dispatch a SECOND worker.
            if start["state"] == "failed":
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 f"start {session_id} already failed; it may not acquire a "
                                 f"session")
            # Construct the record as VALIDATION before writing (rev 4's scalar API dropped
            # this, and SQLite's TEXT affinity would coerce the mistake into durable state).
            # Identity comes from the start, so it cannot disagree.
            SessionRecord(session_id=session_id, owner_id=start["owner_id"],
                          orchestration_id=start["orchestration_id"],
                          generation_id=start["generation_id"], state=state,
                          executor=executor, task=task, cwd=cwd, model=model,
                          created_at=created_at)
            try:
                self._conn.execute(
                    "INSERT INTO sessions (session_id, state, executor, task, cwd, model, "
                    "created_at, schema_version) VALUES (?,?,?,?,?,?,?,?)",
                    (session_id, state, executor, task, cwd, model, created_at,
                     SCHEMA_VERSION))
            except sqlite3.IntegrityError as e:
                if "UNIQUE" in str(e) or "PRIMARY KEY" in str(e):
                    raise NelixError(DUPLICATE_START,
                                     f"session already exists: {session_id}") from None
                raise NelixError(STORE_CORRUPT, f"session insert failed: {e}") from None

    @translates_sqlite
    def transition_session(self, session_id: str, *, owner_id: str, state: str,
                           expected_state=None) -> None:
        """Move ONLY the state, and only for the owner. Everything else is identity.

        `state` is validated here because SQLite's TEXT affinity would silently coerce 42 to
        '42' and round-trip it forever — defeating the records layer's guarantee that a
        malformed field surfaces at its cause.

        `expected_state` makes the write a compare-and-set: without it, two concurrent
        transitions are last-writer-wins and a stale one can resurrect a finished session.
        """
        if not isinstance(state, str) or not state:
            raise NelixError(INVALID_REQUEST, f"state must be a non-empty string: {state!r}")
        if expected_state is None:
            cur = self._conn.execute(
                f"UPDATE sessions SET state=? WHERE session_id=? AND {_OWNS_SESSION}",
                (state, session_id, owner_id))
        else:
            cur = self._conn.execute(
                f"UPDATE sessions SET state=? WHERE session_id=? AND {_OWNS_SESSION} "
                "AND state=?",
                (state, session_id, owner_id, expected_state))
        if cur.rowcount:
            return
        self.get_session(session_id, owner_id=owner_id)   # raises UNKNOWN_SESSION / OWNER_MISMATCH
        raise NelixError(IDEMPOTENCY_CONFLICT,
                         f"{session_id} is not in the expected state {expected_state!r}")

    @translates_sqlite
    def get_session(self, session_id: str, *, owner_id: str) -> SessionRecord:
        row = self._conn.execute(f"{_SESSION_SELECT} WHERE s.session_id=?",
                                 (session_id,)).fetchone()
        if row is None:
            raise NelixError(UNKNOWN_SESSION, f"no such session: {session_id}")
        if row["owner_id"] != owner_id:
            raise NelixError(OWNER_MISMATCH, "session belongs to another owner")
        return _decode_stored(SessionRecord, row)   # fails closed on a future schema

    @translates_sqlite
    def list_sessions(self, owner_id: str) -> list:
        rows = self._conn.execute(
            f"{_SESSION_SELECT} WHERE st.owner_id=? AND s.schema_version=? "
            "ORDER BY s.created_at, s.session_id", (owner_id, SCHEMA_VERSION)).fetchall()
        records, _skipped = _read_rows(rows, SessionRecord)
        return records

    # ---- terminal records -----------------------------------------------------
    # These OUTLIVE their generation: the record must be here before the live session is
    # removed (design §5's ordering invariant).
    @translates_sqlite
    def put_terminal(self, session_id: str, *, terminal_kind: str, summary: str,
                     ended_at: float) -> None:
        """Publish the terminal result. Identity is the session's.

        Idempotent for the SAME result; a DIFFERENT result is a conflict — the same policy
        ledger.fail() already applies to the same question. Silently discarding a
        conflicting retry (rev 4) reports success while keeping the old result, and no
        higher layer can repair that afterwards.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            start = self._conn.execute(
                "SELECT st.owner_id, st.orchestration_id, st.generation_id FROM sessions s "
                "JOIN starts st ON st.session_id = s.session_id WHERE s.session_id=?",
                (session_id,)).fetchone()
            if start is None:
                raise NelixError(UNKNOWN_SESSION, f"no such session: {session_id}")
            # Validate before writing; identity from the join cannot disagree.
            TerminalRecord(session_id=session_id, owner_id=start["owner_id"],
                           orchestration_id=start["orchestration_id"],
                           generation_id=start["generation_id"],
                           terminal_kind=terminal_kind, summary=summary, ended_at=ended_at)
            existing = self._conn.execute(
                "SELECT terminal_kind, summary, ended_at FROM terminal WHERE session_id=?",
                (session_id,)).fetchone()
            if existing is not None:
                if (existing["terminal_kind"], existing["summary"], existing["ended_at"]) == (
                        terminal_kind, summary, ended_at):
                    return                      # same result: idempotent, ack untouched
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 f"{session_id} already ended as {existing['terminal_kind']!r}")
            self._conn.execute(
                "INSERT INTO terminal (session_id, terminal_kind, summary, ended_at, "
                "acknowledged_at, schema_version) VALUES (?,?,?,?,?,?)",
                (session_id, terminal_kind, summary, ended_at, None, SCHEMA_VERSION))

    @translates_sqlite
    def get_terminal(self, session_id: str, *, owner_id: str) -> TerminalRecord:
        row = self._conn.execute(f"{_TERMINAL_SELECT} WHERE t.session_id=?",
                                 (session_id,)).fetchone()
        if row is None:
            raise NelixError(UNKNOWN_SESSION, f"no terminal record: {session_id}")
        if row["owner_id"] != owner_id:
            raise NelixError(OWNER_MISMATCH, "session belongs to another owner")
        return _decode_stored(TerminalRecord, row)

    @translates_sqlite
    def list_terminal(self, owner_id: str) -> list:
        rows = self._conn.execute(
            f"{_TERMINAL_SELECT} WHERE st.owner_id=? AND t.schema_version=? "
            "ORDER BY t.ended_at, t.session_id", (owner_id, SCHEMA_VERSION)).fetchall()
        records, _skipped = _read_rows(rows, TerminalRecord)
        return records

    @translates_sqlite
    def ack_terminal(self, session_id: str, *, owner_id: str) -> TerminalRecord:
        """Idempotent: a repeated ack returns the SAME record with its ORIGINAL timestamp.

        The whole operation is one transaction: rev 4 read, CAS'd and re-read without one, so
        a prune landing between the CAS and the re-read made an ack that DURABLY SUCCEEDED
        report unknown_session.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            record = self.get_terminal(session_id, owner_id=owner_id)   # owner guard first
            if record.acknowledged_at is not None:
                return record
            self._conn.execute(
                "UPDATE terminal SET acknowledged_at=? WHERE session_id=? "
                "AND acknowledged_at IS NULL", (float(self._clock()), session_id))
            return self.get_terminal(session_id, owner_id=owner_id)

    @translates_sqlite
    def prune_terminal(self, *, max_age_seconds: float, max_count: int) -> int:
        """Drop acknowledged records; bound the rest by age, and by count PER OWNER.

        Per owner is not a detail: a global count bound lets a noisy owner evict a quiet
        owner's unacknowledged result — which breaks both "unacked results survive" and
        "owner is a correctness namespace". rev 1 did exactly that.

        Deliberately operates on ROWS, not deserialised records, so a row from a newer
        schema is still bounded rather than growing forever behind a read this build cannot
        perform.
        """
        if (isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float))
                or not math.isfinite(max_age_seconds) or max_age_seconds < 0):
            raise NelixError(INVALID_REQUEST,
                             f"max_age_seconds must be a finite, non-negative number: "
                             f"{max_age_seconds!r}")
        if isinstance(max_count, bool) or not isinstance(max_count, int) or max_count < 0:
            raise NelixError(INVALID_REQUEST, "max_count must be a non-negative int")
        # Validating max_age_seconds above and then reading `now` from an UNCHECKED clock
        # leaves the identical hole: a NaN now makes every (now - ended_at) > max_age
        # comparison False, so nothing is ever reaped by age while the bound looks
        # configured. +inf is worse — it reaps every record, acknowledged or not. Same rule
        # as every stored timestamp, so it is the same helper, not a second copy of it.
        # invalid_request names the right party: the clock is this Store's own construction
        # argument, and no retry of the same call can fix it (retryable=False).
        now = timestamp(self._clock(), "clock")
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            removed = self._conn.execute(
                "DELETE FROM terminal WHERE acknowledged_at IS NOT NULL "
                "OR (? - ended_at) > ?", (now, max_age_seconds)).rowcount
            removed += self._conn.execute(
                "DELETE FROM terminal WHERE session_id IN ("
                "  SELECT session_id FROM ("
                "    SELECT t.session_id, ROW_NUMBER() OVER ("
                "      PARTITION BY st.owner_id ORDER BY t.ended_at DESC, t.session_id DESC"
                "    ) AS rn FROM terminal t "
                "    JOIN starts st ON st.session_id = t.session_id"
                "  ) WHERE rn > ?)", (max_count,)).rowcount
        return removed
