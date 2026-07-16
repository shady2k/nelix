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
import sqlite3
import time

from nelix_contracts.errors import (
    DUPLICATE_START, IDEMPOTENCY_CONFLICT, INVALID_REQUEST, OWNER_MISMATCH, STORE_CORRUPT,
    UNKNOWN_SESSION, NelixError,
)
from nelix_contracts.records import SCHEMA_VERSION, SessionRecord, TerminalRecord

from .db import connect

_SESSION_COLS = ("session_id, owner_id, orchestration_id, generation_id, state, executor, "
                 "task, cwd, model, created_at, schema_version")
_TERMINAL_COLS = ("session_id, owner_id, orchestration_id, generation_id, terminal_kind, "
                  "summary, ended_at, acknowledged_at, schema_version")


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


class Store:
    def __init__(self, root, *, clock=time.time):
        self._conn = connect(root)
        self._clock = clock

    def close(self):
        self._conn.close()

    # ---- sessions -------------------------------------------------------------
    def create_session(self, record: SessionRecord) -> None:
        """Exclusive create. NEVER an overwrite: identity (owner, orchestration, generation,
        task, cwd, created_at) is immutable, and a blind replace could hand the session to a
        different owner."""
        try:
            self._conn.execute(
                f"INSERT INTO sessions ({_SESSION_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (record.session_id, record.owner_id, record.orchestration_id,
                 record.generation_id, record.state, record.executor, record.task,
                 record.cwd, record.model, record.created_at, record.schema_version))
        except sqlite3.IntegrityError as e:
            # Only a PK conflict is a duplicate start. Anything else (NOT NULL, CHECK) is a
            # bug in the caller's record, and telling them "already exists" would send them
            # down the wrong path.
            if "UNIQUE" not in str(e) and "PRIMARY KEY" not in str(e):
                raise NelixError(STORE_CORRUPT, f"session insert failed: {e}") from None
            raise NelixError(DUPLICATE_START,
                             f"session already exists: {record.session_id}") from None

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
                "UPDATE sessions SET state=? WHERE session_id=? AND owner_id=?",
                (state, session_id, owner_id))
        else:
            cur = self._conn.execute(
                "UPDATE sessions SET state=? WHERE session_id=? AND owner_id=? AND state=?",
                (state, session_id, owner_id, expected_state))
        if cur.rowcount:
            return
        self.get_session(session_id, owner_id=owner_id)   # raises UNKNOWN_SESSION / OWNER_MISMATCH
        raise NelixError(IDEMPOTENCY_CONFLICT,
                         f"{session_id} is not in the expected state {expected_state!r}")

    def get_session(self, session_id: str, *, owner_id: str) -> SessionRecord:
        row = self._conn.execute(
            f"SELECT {_SESSION_COLS} FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise NelixError(UNKNOWN_SESSION, f"no such session: {session_id}")
        if row["owner_id"] != owner_id:
            raise NelixError(OWNER_MISMATCH, "session belongs to another owner")
        return SessionRecord.from_dict(dict(row))   # fails closed on a future schema

    def list_sessions(self, owner_id: str) -> list:
        rows = self._conn.execute(
            f"SELECT {_SESSION_COLS} FROM sessions WHERE owner_id=? AND schema_version=? "
            "ORDER BY created_at, session_id", (owner_id, SCHEMA_VERSION)).fetchall()
        records, _skipped = _read_rows(rows, SessionRecord)
        return records

    # ---- terminal records -----------------------------------------------------
    # These OUTLIVE their generation: the record must be here before the live session is
    # removed (design §5's ordering invariant).
    def put_terminal(self, record: TerminalRecord) -> None:
        """Insert-if-absent. A re-published record must never erase an acknowledgement the
        owner already made."""
        self._conn.execute(
            f"INSERT INTO terminal ({_TERMINAL_COLS}) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO NOTHING",
            (record.session_id, record.owner_id, record.orchestration_id,
             record.generation_id, record.terminal_kind, record.summary,
             record.ended_at, record.acknowledged_at, record.schema_version))

    def get_terminal(self, session_id: str, *, owner_id: str) -> TerminalRecord:
        row = self._conn.execute(
            f"SELECT {_TERMINAL_COLS} FROM terminal WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise NelixError(UNKNOWN_SESSION, f"no terminal record: {session_id}")
        if row["owner_id"] != owner_id:
            raise NelixError(OWNER_MISMATCH, "session belongs to another owner")
        return TerminalRecord.from_dict(dict(row))

    def list_terminal(self, owner_id: str) -> list:
        rows = self._conn.execute(
            f"SELECT {_TERMINAL_COLS} FROM terminal WHERE owner_id=? AND schema_version=? "
            "ORDER BY ended_at, session_id", (owner_id, SCHEMA_VERSION)).fetchall()
        records, _skipped = _read_rows(rows, TerminalRecord)
        return records

    def ack_terminal(self, session_id: str, *, owner_id: str) -> TerminalRecord:
        """Compare-and-set, so concurrent acks agree on ONE timestamp: the UPDATE only fires
        while acknowledged_at IS NULL. rev 1 read-modify-wrote this and the later writer won.
        """
        record = self.get_terminal(session_id, owner_id=owner_id)   # owner guard first
        if record.acknowledged_at is not None:
            return record
        self._conn.execute(
            "UPDATE terminal SET acknowledged_at=? WHERE session_id=? "
            "AND acknowledged_at IS NULL", (float(self._clock()), session_id))
        # Re-read: whoever won the CAS, everyone returns the same stamp.
        return self.get_terminal(session_id, owner_id=owner_id)

    def prune_terminal(self, *, max_age_seconds: float, max_count: int) -> int:
        """Drop acknowledged records; bound the rest by age, and by count PER OWNER.

        Per owner is not a detail: a global count bound lets a noisy owner evict a quiet
        owner's unacknowledged result — which breaks both "unacked results survive" and
        "owner is a correctness namespace". rev 1 did exactly that.

        Deliberately operates on ROWS, not deserialised records, so a row from a newer
        schema is still bounded rather than growing forever behind a read this build cannot
        perform.
        """
        if not isinstance(max_age_seconds, (int, float)) or max_age_seconds < 0:
            raise NelixError(INVALID_REQUEST, "max_age_seconds must be >= 0")
        if isinstance(max_count, bool) or not isinstance(max_count, int) or max_count < 0:
            raise NelixError(INVALID_REQUEST, "max_count must be a non-negative int")
        now = float(self._clock())
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            removed = self._conn.execute(
                "DELETE FROM terminal WHERE acknowledged_at IS NOT NULL "
                "OR (? - ended_at) > ?", (now, max_age_seconds)).rowcount
            removed += self._conn.execute(
                "DELETE FROM terminal WHERE session_id IN ("
                "  SELECT session_id FROM ("
                "    SELECT session_id, ROW_NUMBER() OVER ("
                "      PARTITION BY owner_id ORDER BY ended_at DESC, session_id DESC"
                "    ) AS rn FROM terminal"
                "  ) WHERE rn > ?)", (max_count,)).rowcount
        return removed
