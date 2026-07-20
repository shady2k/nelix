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
    STORE_CORRUPT, TERMINAL_EXPIRED, UNKNOWN_SESSION, NelixError,
)
from nelix_contracts.records import (
    SCHEMA_VERSION, GenerationRecord, EpochRecord, SessionRecord, TerminalRecord,
    timestamp,
)

from .db import ThreadLocalConnections, translates_sqlite

# Identity is JOINED from starts, never stored in these tables (nelix-555). Three
# independent copies could disagree; one row cannot disagree with itself.
_SESSION_SELECT = (
    "SELECT s.session_id, st.owner_id, st.orchestration_id, st.generation_id, "
    "st.generation_epoch, s.state, "
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
    "st.generation_epoch, "
    "t.terminal_kind, t.summary, t.ended_at, t.published_at, t.terminal_seq, "
    "t.acknowledged_at, "
    "t.expired_at, t.expire_reason, t.schema_version "
    "FROM terminal t JOIN starts st ON st.session_id = t.session_id")

# A row the OWNER still has to deal with. Dismissal (ack, theirs, at once) and expiry (prune,
# ours, later) both take a result off the board; neither deletes the receipt underneath it.
_ON_THE_BOARD = "t.acknowledged_at IS NULL AND t.expired_at IS NULL"

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
        self._conns = ThreadLocalConnections(root, timeout=timeout)
        # Opened NOW, in the constructing thread: every existing caller relies on
        # construction itself validating/creating the database synchronously (a bad root,
        # an unsupported sqlite, a disagreeing schema all raise here, not on first use).
        # Any OTHER thread that later touches this same instance still gets its own
        # connection, opened lazily on ITS first use — see ThreadLocalConnections.
        self._conns.get()
        self._clock = clock

    @property
    def _conn(self):
        """The CALLING thread's own connection (opened lazily if this is its first use)."""
        return self._conns.get()

    @translates_sqlite
    def close(self):
        self._conns.close()

    # ---- board_seq helpers (S2a.1) -------------------------------------------
    @staticmethod
    def _bump_board_seq(conn, owner_id: str) -> None:
        """Increment the owner's board_seq by 1, creating the row if absent.

        Idempotent: called once per owner per transaction inside the same
        BEGIN IMMEDIATE. The calling method must NOT call this for owners whose
        rows did not actually change.
        """
        conn.execute(
            "INSERT INTO owner_board_seq(owner_id, seq) VALUES (?, 1) "
            "ON CONFLICT(owner_id) DO UPDATE SET seq = seq + 1",
            (owner_id,))

    @translates_sqlite
    def get_owner_board_seq(self, owner_id: str) -> int:
        """Return the owner's current board_seq (0 if never mutated)."""
        if not isinstance(owner_id, str) or not owner_id:
            raise NelixError(INVALID_REQUEST,
                             f"owner_id must be a non-empty string: {owner_id!r}")
        row = self._conn.execute(
            "SELECT seq FROM owner_board_seq WHERE owner_id=?", (owner_id,)).fetchone()
        return row[0] if row else 0

    @translates_sqlite
    def read_board_snapshot(self, owner_id: str):
        """Atomically return (board_seq, rows) in ONE explicit read transaction.

        Connections are isolation_level=None so two bare SELECTs are NOT one
        snapshot.  This method explicitly BEGINs, reads the owner's board_seq
        (0 if no row), reads the owner's archived board (same shape as
        list_terminal), and commits — giving the cursor-before-snapshot
        invariant: any mutation before the commit's snapshot has a board_seq <=
        the returned high-water, and any mutation after has a strictly larger
        board_seq.
        """
        if not isinstance(owner_id, str) or not owner_id:
            raise NelixError(INVALID_REQUEST,
                             f"owner_id must be a non-empty string: {owner_id!r}")
        with self._conn:
            self._conn.execute("BEGIN")
            seq = self.get_owner_board_seq(owner_id)
            rows = self.list_terminal(owner_id)
        return seq, rows

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
                "SELECT owner_id, orchestration_id, generation_id, generation_epoch, state "
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
                          generation_id=start["generation_id"],
                          generation_epoch=start["generation_epoch"], state=state,
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

        Assigns a per-generation monotonic terminal_seq atomically inside the transaction,
        so the caller (daemon/manager.py _free_slot) does NOT need to change.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            start = self._conn.execute(
                "SELECT st.owner_id, st.orchestration_id, st.generation_id, "
                "st.generation_epoch FROM sessions s "
                "JOIN starts st ON st.session_id = s.session_id WHERE s.session_id=?",
                (session_id,)).fetchone()
            if start is None:
                raise NelixError(UNKNOWN_SESSION, f"no such session: {session_id}")
            # The store's own stamp: retention ages from THIS, never from the caller's
            # ended_at. Read before the comparison below, not after, because the incoming
            # record must be judged valid before any stored state is consulted — otherwise a
            # malformed field on a session that already ended reports IDEMPOTENCY_CONFLICT
            # ("you sent a different result") for what is really INVALID_REQUEST ("you sent
            # nonsense"), naming the wrong disagreement. The cost is that a Store built with a
            # nonsense clock now fails EVERY put_terminal including an otherwise-idempotent
            # retry — deliberate: such a Store cannot publish anything, and one answer for one
            # call beats an answer that depends on whether a row happens to exist.
            published_at = timestamp(self._clock(), "clock")
            generation_id = start["generation_id"]
            generation_epoch = start["generation_epoch"]
            # Validate before writing; identity from the join cannot disagree.
            TerminalRecord(session_id=session_id, owner_id=start["owner_id"],
                           orchestration_id=start["orchestration_id"],
                           generation_id=generation_id,
                           generation_epoch=generation_epoch,
                           terminal_kind=terminal_kind, summary=summary, ended_at=ended_at,
                           published_at=published_at)
            existing = self._conn.execute(
                "SELECT terminal_kind, summary, ended_at FROM terminal WHERE session_id=?",
                (session_id,)).fetchone()
            if existing is not None:
                if (existing["terminal_kind"], existing["summary"], existing["ended_at"]) == (
                        terminal_kind, summary, ended_at):
                    # Same result: idempotent. published_at is NOT restamped — a generation
                    # retrying in a loop would otherwise keep its result alive forever and
                    # defeat the age bound retention just moved to.
                    return
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 f"{session_id} already ended as {existing['terminal_kind']!r}")
            # Atomically assign a per-generation terminal_seq. Initialize the counter if this
            # is the first terminal for this epoch (INSERT OR IGNORE), then increment.
            self._conn.execute(
                "INSERT OR IGNORE INTO generation_progress "
                "(generation_id, next_terminal_seq) "
                "VALUES (?, 1)", (generation_epoch,))
            self._conn.execute(
                "UPDATE generation_progress SET next_terminal_seq = next_terminal_seq + 1 "
                "WHERE generation_id=?", (generation_epoch,))
            row = self._conn.execute(
                "SELECT next_terminal_seq - 1 AS terminal_seq FROM generation_progress "
                "WHERE generation_id=?", (generation_epoch,)).fetchone()
            terminal_seq = row["terminal_seq"]
            self._conn.execute(
                "INSERT INTO terminal (session_id, terminal_kind, summary, ended_at, "
                "published_at, terminal_seq, acknowledged_at, schema_version) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (session_id, terminal_kind, summary, ended_at, published_at, terminal_seq,
                 None, SCHEMA_VERSION))
            self._bump_board_seq(self._conn, start["owner_id"])

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
        """The owner's BOARD: results awaiting their attention.

        `AND t.acknowledged_at IS NULL` is what makes ack mean anything. Without it this
        filtered on owner and schema_version alone, so an acknowledged result stayed on the
        board until the pruner happened to run — "acknowledge" meant "dismiss, eventually, on
        the GC's schedule". Dismissal (the owner's decision, at once) and reclamation (the
        pruner's, later) are different events.

        `AND t.expired_at IS NULL` is the second half, and it is what lets prune stop deleting:
        an expired result leaves the board without its receipt leaving the database.

        The filter is HERE and not in _TERMINAL_SELECT on purpose: get_terminal shares that
        SELECT, a get by id is not the board, and ack_terminal re-reads through get_terminal
        inside its own transaction — hiding acked rows there would break ack's idempotency
        and the ack/prune seam with it.
        """
        rows = self._conn.execute(
            f"{_TERMINAL_SELECT} WHERE st.owner_id=? AND t.schema_version=? "
            f"AND {_ON_THE_BOARD} "
            "ORDER BY t.ended_at, t.session_id", (owner_id, SCHEMA_VERSION)).fetchall()
        records, _skipped = _read_rows(rows, TerminalRecord)
        return records

    @translates_sqlite
    def get_generation_persisted_high_water(self, generation_epoch: str) -> int:
        """Return the highest terminal_seq persisted for this epoch (0 if none).

        Used by the retirement oracle: terminal_persisted_high_water must be compared against
        router_visible_high_water (the highest seq the router has confirmed). An epoch
        with no terminals returns 0 so it is immediately "watermark-satisfied".
        """
        if not isinstance(generation_epoch, str) or not generation_epoch:
            raise NelixError(INVALID_REQUEST,
                             f"generation_epoch must be a non-empty string: {generation_epoch!r}")
        row = self._conn.execute(
            "SELECT MAX(t.terminal_seq) AS hw FROM terminal t "
            "JOIN starts st ON st.session_id = t.session_id "
            "WHERE st.generation_epoch=?", (generation_epoch,)).fetchone()
        return row["hw"] if row and row["hw"] is not None else 0

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
            # An expired result is durably retired and cannot be dismissed. Naming it is the
            # point: prune used to DELETE the row, so this answered unknown_session — the same
            # answer as a session id that was never real, for the opposite situation. A caller
            # cannot tell "you were too late" from "you are confused" and the two want opposite
            # responses. The table's `expired_at IS NULL OR acknowledged_at IS NULL` CHECK
            # backstops this branch: without it the CAS below would try to acknowledge an
            # expired row and SQLite would refuse the write outright.
            if record.expired_at is not None:
                raise NelixError(TERMINAL_EXPIRED,
                                 f"{session_id} expired ({record.expire_reason}) before it was "
                                 f"acknowledged")
            # SQLite silently COERCES NaN to NULL on write. `float(self._clock())` therefore
            # let a NaN clock stamp NULL — leaving this CAS's own "AND acknowledged_at IS
            # NULL" guard still matching, the re-read returning None (VALID, the field is
            # optional), nothing raising, and the transaction COMMITTING. Measured: ack
            # reported SUCCESS and acknowledged nothing, silently and forever. +inf failed the
            # opposite way (stored as-is, rejected by the re-read, rolled back) and reported
            # OUR clock as store_corrupt. Same rule as every other timestamp, so the same
            # helper — read here rather than at entry because a REPEATED ack returns above
            # without needing a clock at all, and must stay idempotent regardless of one.
            self._conn.execute(
                "UPDATE terminal SET acknowledged_at=? WHERE session_id=? "
                "AND acknowledged_at IS NULL", (timestamp(self._clock(), "clock"), session_id))
            self._bump_board_seq(self._conn, owner_id)
            return self.get_terminal(session_id, owner_id=owner_id)

    @translates_sqlite
    def prune_terminal(self, *, max_age_seconds: float, max_count: int) -> int:
        """Bound the BOARD by age and by count PER OWNER. Returns the number of results retired.

        This DELETED rows, and that was the defect: the row is the only evidence that a session
        ever ended, so deleting it made the store forget, and the next matching retry re-published
        the owner's dismissed result onto their board. It now stamps `expired_at`, which takes the
        result off the board and leaves the receipt. Nothing here deletes anything: a receipt lives
        at least as long as its session and start, and reclaiming all three is a session-history GC
        that does not exist yet.

        It never touches an ACKNOWLEDGED row. Under the old condition — `acknowledged_at IS NOT
        NULL OR (age)` — max_age gated only unacknowledged rows, so an acked row was eligible on
        the very next prune at any age: the ack->prune window was zero, which is why an ordinary
        sub-second retry could land in it. An acked result is already off the board and there is
        nothing left to reclaim from it.

        Per owner is not a detail: a global count bound lets a noisy owner evict a quiet
        owner's unacknowledged result — which breaks both "unacked results survive" and
        "owner is a correctness namespace". rev 1 did exactly that. The count bounds the BOARD,
        so only board rows are counted — receipts an owner has already dealt with must not evict
        the live results they have not.

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
            # `AS t` so the one _ON_THE_BOARD predicate reads verbatim here, in the count
            # query's subquery below, and on the board itself: three hand-written copies of
            # "what is on the board" is three chances for them to drift, and a drift between
            # the board's filter and the pruner's would either strand rows on the board forever
            # or retire ones still being shown.
            expired = self._conn.execute(
                "UPDATE terminal AS t SET expired_at=?, expire_reason='age' "
                f"WHERE {_ON_THE_BOARD} AND (? - t.published_at) > ? "
                "RETURNING session_id",
                (now, now, max_age_seconds)).fetchall()
            # Runs AFTER the age pass, and reads what that pass wrote: rows it just expired are
            # no longer on the board, so they neither survive the count bound nor get counted
            # twice into the return value.
            expired += self._conn.execute(
                "UPDATE terminal SET expired_at=?, expire_reason='count' "
                "WHERE session_id IN ("
                "  SELECT session_id FROM ("
                "    SELECT t.session_id, ROW_NUMBER() OVER ("
                "      PARTITION BY st.owner_id ORDER BY t.published_at DESC, t.session_id DESC"
                "    ) AS rn FROM terminal t "
                "    JOIN starts st ON st.session_id = t.session_id "
                f"    WHERE {_ON_THE_BOARD}"
                "  ) WHERE rn > ?) RETURNING session_id",
                (now, max_count)).fetchall()
            # Bump board_seq for EVERY distinct owner whose rows actually changed
            # in either pass, exactly once each. Collect session_ids from both
            # RETURNING result sets (they contain only rows THIS call just changed).
            all_ids = tuple(r["session_id"] for batch in (expired, ) for r in batch)
            n_expired = len(all_ids)
            if all_ids:
                placeholders = ",".join("?" * len(all_ids))
                affected = self._conn.execute(
                    f"SELECT DISTINCT st.owner_id FROM terminal t "
                    f"JOIN starts st ON st.session_id = t.session_id "
                    f"WHERE t.session_id IN ({placeholders})",
                    all_ids).fetchall()
                for row in affected:
                    self._bump_board_seq(self._conn, row["owner_id"])
        return n_expired

    # ---- generations/epochs identity API (nelix-80e-s1a) ----

    @translates_sqlite
    def create_generation(self, generation_id: str, *, build_id, lifecycle_state: str,
                          capability_snapshot, created_at: float) -> None:
        """Insert a generations row with current_epoch=NULL."""
        if not isinstance(generation_id, str) or not generation_id:
            raise NelixError(INVALID_REQUEST,
                             f"generation_id must be a non-empty string: {generation_id!r}")
        if build_id is not None and (not isinstance(build_id, str) or not build_id):
            raise NelixError(INVALID_REQUEST, f"build_id must be a non-empty string or None: {build_id!r}")
        if not isinstance(lifecycle_state, str) or not lifecycle_state:
            raise NelixError(INVALID_REQUEST,
                             f"lifecycle_state must be a non-empty string: {lifecycle_state!r}")
        if capability_snapshot is not None and (not isinstance(capability_snapshot, str)
                                                or not capability_snapshot):
            raise NelixError(INVALID_REQUEST,
                             f"capability_snapshot must be a non-empty string or None: "
                             f"{capability_snapshot!r}")
        created_at = timestamp(created_at, "created_at")
        GenerationRecord(generation_id=generation_id, build_id=build_id,
                         lifecycle_state=lifecycle_state, current_epoch=None,
                         capability_snapshot=capability_snapshot, created_at=created_at)
        try:
            self._conn.execute(
                "INSERT INTO generations (generation_id, build_id, lifecycle_state, "
                "current_epoch, capability_snapshot, created_at) VALUES (?,?,?,?,?,?)",
                (generation_id, build_id, lifecycle_state, None,
                 capability_snapshot, created_at))
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e) or "PRIMARY KEY" in str(e):
                raise NelixError(DUPLICATE_START,
                                 f"generation already exists: {generation_id}") from None
            raise

    @translates_sqlite
    def insert_epoch(self, generation_epoch: str, generation_id: str, *,
                     incarnation_meta, created_at: float) -> None:
        """Insert an epochs row with process_state='starting', retirement_state='open'."""
        for name, val in [("generation_epoch", generation_epoch),
                          ("generation_id", generation_id)]:
            if not isinstance(val, str) or not val:
                raise NelixError(INVALID_REQUEST,
                                 f"{name} must be a non-empty string: {val!r}")
        if incarnation_meta is not None and (not isinstance(incarnation_meta, str)
                                             or not incarnation_meta):
            raise NelixError(INVALID_REQUEST,
                             f"incarnation_meta must be a non-empty string or None: "
                             f"{incarnation_meta!r}")
        created_at = timestamp(created_at, "created_at")
        EpochRecord(generation_epoch=generation_epoch, generation_id=generation_id,
                    process_state="starting", retirement_state="open",
                    certificate=None, final_high_water=None,
                    incarnation_meta=incarnation_meta, created_at=created_at)
        try:
            self._conn.execute(
                "INSERT INTO epochs (generation_epoch, generation_id, process_state, "
                "retirement_state, certificate, final_high_water, incarnation_meta, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (generation_epoch, generation_id, "starting", "open",
                 None, None, incarnation_meta, created_at))
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e) or "PRIMARY KEY" in str(e):
                raise NelixError(DUPLICATE_START,
                                 f"epoch already exists: {generation_epoch}") from None
            if "FOREIGN KEY" in str(e) or "constraint failed" in str(e):
                raise NelixError(UNKNOWN_SESSION,
                                 f"generation not found: {generation_id}") from None
            raise

    @translates_sqlite
    def cas_epoch_serving(self, generation_id: str, generation_epoch: str, *,
                          expected_current_epoch, incarnation_meta=None) -> None:
        """Guarded CAS: promote epoch ``starting → serving`` + set ``generations.current_epoch``
        + record ``incarnation_meta``.

        ALL-OR-NOTHING in one transaction — verifies THREE things before writing:
          1. The epoch BELONGS to the supplied generation (``generation_id`` matches).
          2. The epoch's ``process_state`` is EXACTLY ``starting``.
          3. The generation's ``current_epoch`` matches ``expected_current_epoch``.

        Checks rowcounts on BOTH UPDATEs; rolls back on any miss. Fails if
        any condition is violated (IDEMPOTENCY_CONFLICT) or generation/epoch not found.

        ``incarnation_meta``, when provided, is written to epochs.incarnation_meta
        in the SAME transaction (C7: records the observed incarnation on promotion).
        """
        if not isinstance(generation_id, str) or not generation_id:
            raise NelixError(INVALID_REQUEST,
                             f"generation_id must be a non-empty string: {generation_id!r}")
        if not isinstance(generation_epoch, str) or not generation_epoch:
            raise NelixError(INVALID_REQUEST,
                             f"generation_epoch must be a non-empty string: {generation_epoch!r}")
        if incarnation_meta is not None and (not isinstance(incarnation_meta, str)
                                             or not incarnation_meta):
            raise NelixError(INVALID_REQUEST,
                             f"incarnation_meta must be a non-empty string or None: "
                             f"{incarnation_meta!r}")
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            # Verify 1: generation exists AND current_epoch matches expected.
            row = self._conn.execute(
                "SELECT current_epoch FROM generations WHERE generation_id=?",
                (generation_id,)).fetchone()
            if row is None:
                raise NelixError(UNKNOWN_SESSION,
                                 f"no such generation: {generation_id}")
            cur = row["current_epoch"]
            if cur != expected_current_epoch:
                raise NelixError(
                    IDEMPOTENCY_CONFLICT,
                    f"current_epoch {cur!r} does not match expected "
                    f"{expected_current_epoch!r}")
            # Verify 2: epoch belongs to this generation AND is exactly 'starting'.
            ep_row = self._conn.execute(
                "SELECT process_state FROM epochs "
                "WHERE generation_epoch=? AND generation_id=?",
                (generation_epoch, generation_id)).fetchone()
            if ep_row is None:
                raise NelixError(
                    IDEMPOTENCY_CONFLICT,
                    f"epoch {generation_epoch!r} does not belong to generation "
                    f"{generation_id!r}")
            if ep_row["process_state"] != "starting":
                raise NelixError(
                    IDEMPOTENCY_CONFLICT,
                    f"epoch {generation_epoch!r} is not starting "
                    f"(actual: {ep_row['process_state']!r})")
            # Now: update BOTH (or THREE with incarnation_meta), check rowcounts.
            rc1 = self._conn.execute(
                "UPDATE generations SET current_epoch=? WHERE generation_id=?",
                (generation_epoch, generation_id)).rowcount
            if incarnation_meta is not None:
                rc2 = self._conn.execute(
                    "UPDATE epochs SET process_state='serving', incarnation_meta=? "
                    "WHERE generation_epoch=? AND generation_id=?",
                    (incarnation_meta, generation_epoch, generation_id)).rowcount
            else:
                rc2 = self._conn.execute(
                    "UPDATE epochs SET process_state='serving' "
                    "WHERE generation_epoch=? AND generation_id=?",
                    (generation_epoch, generation_id)).rowcount
            if rc1 != 1 or rc2 != 1:
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 "CAS: rowcount mismatch during promotion")

    @translates_sqlite
    def set_epoch_process_state(self, generation_epoch: str, state: str) -> None:
        """Update an epoch's process_state (guarded transition: starting|serving|dead)."""
        if not isinstance(generation_epoch, str) or not generation_epoch:
            raise NelixError(INVALID_REQUEST,
                             f"generation_epoch must be a non-empty string: {generation_epoch!r}")
        if state not in ("starting", "serving", "dead"):
            raise NelixError(INVALID_REQUEST,
                             f"invalid process_state: {state!r}")
        cur = self._conn.execute(
            "UPDATE epochs SET process_state=? WHERE generation_epoch=?",
            (state, generation_epoch))
        if cur.rowcount == 0:
            raise NelixError(UNKNOWN_SESSION,
                             f"no such epoch: {generation_epoch}")

    @translates_sqlite
    def get_epoch_retirement_state(self, generation_epoch: str) -> str:
        """Return the retirement_state for an epoch ('open', 'quiescing', or 'certified')."""
        if not isinstance(generation_epoch, str) or not generation_epoch:
            raise NelixError(INVALID_REQUEST,
                             f"generation_epoch must be a non-empty string: {generation_epoch!r}")
        row = self._conn.execute(
            "SELECT retirement_state FROM epochs WHERE generation_epoch=?",
            (generation_epoch,)).fetchone()
        if row is None:
            raise NelixError(UNKNOWN_SESSION, f"no such epoch: {generation_epoch}")
        return row["retirement_state"]

    @translates_sqlite
    def set_epoch_retirement(self, generation_epoch: str, *,
                             retirement_state: str, certificate=None,
                             final_high_water=None) -> None:
        """Update an epoch's retirement_state (guarded: open|quiescing|certified).

        When `certified`, also writes certificate and final_high_water.
        """
        if not isinstance(generation_epoch, str) or not generation_epoch:
            raise NelixError(INVALID_REQUEST,
                             f"generation_epoch must be a non-empty string: {generation_epoch!r}")
        if retirement_state not in ("open", "quiescing", "certified"):
            raise NelixError(INVALID_REQUEST,
                             f"invalid retirement_state: {retirement_state!r}")
        if final_high_water is not None:
            if isinstance(final_high_water, bool) or not isinstance(
                    final_high_water, int) or final_high_water < 0:
                raise NelixError(
                    INVALID_REQUEST,
                    f"final_high_water must be a non-negative int: {final_high_water!r}")
        if retirement_state == "certified":
            cur = self._conn.execute(
                "UPDATE epochs SET retirement_state=?, certificate=?, "
                "final_high_water=? WHERE generation_epoch=?",
                (retirement_state, certificate, final_high_water, generation_epoch))
        else:
            cur = self._conn.execute(
                "UPDATE epochs SET retirement_state=? WHERE generation_epoch=?",
                (retirement_state, generation_epoch))
        if cur.rowcount == 0:
            raise NelixError(UNKNOWN_SESSION,
                             f"no such epoch: {generation_epoch}")

    @translates_sqlite
    def list_epochs(self, generation_id: str) -> list:
        """List epochs for a generation, ordered by created_at.
        SKIPS malformed rows (board semantics: one bad row must not blind the caller)."""
        if not isinstance(generation_id, str) or not generation_id:
            raise NelixError(INVALID_REQUEST,
                             f"generation_id must be a non-empty string: {generation_id!r}")
        rows = self._conn.execute(
            "SELECT generation_epoch, generation_id, process_state, retirement_state, "
            "certificate, final_high_water, incarnation_meta, created_at "
            "FROM epochs WHERE generation_id=? ORDER BY created_at",
            (generation_id,)).fetchall()
        records, _skipped = _read_rows(rows, EpochRecord)
        return records

    @translates_sqlite
    def list_epochs_strict(self, generation_id: str) -> list:
        """List epochs for a generation, ordered by created_at.
        FAIL-CLOSED on any malformed row (authority semantics: reconciliation must not
        skip a row that may hold critical state). Uses _decode_stored instead of _read_rows."""
        if not isinstance(generation_id, str) or not generation_id:
            raise NelixError(INVALID_REQUEST,
                             f"generation_id must be a non-empty string: {generation_id!r}")
        rows = self._conn.execute(
            "SELECT generation_epoch, generation_id, process_state, retirement_state, "
            "certificate, final_high_water, incarnation_meta, created_at "
            "FROM epochs WHERE generation_id=? ORDER BY created_at",
            (generation_id,)).fetchall()
        out = []
        for row in rows:
            out.append(_decode_stored(EpochRecord, row))
        return out

    @translates_sqlite
    def get_generation(self, generation_id: str) -> GenerationRecord:
        """Return a generation row or UNKNOWN_SESSION."""
        if not isinstance(generation_id, str) or not generation_id:
            raise NelixError(INVALID_REQUEST,
                             f"generation_id must be a non-empty string: {generation_id!r}")
        row = self._conn.execute(
            "SELECT generation_id, build_id, lifecycle_state, current_epoch, "
            "capability_snapshot, created_at "
            "FROM generations WHERE generation_id=?",
            (generation_id,)).fetchone()
        if row is None:
            raise NelixError(UNKNOWN_SESSION,
                             f"no such generation: {generation_id}")
        return _decode_stored(GenerationRecord, row)

    @translates_sqlite
    def set_generation_lifecycle_state(self, generation_id: str, state: str) -> None:
        """Update a generation's lifecycle_state."""
        if not isinstance(generation_id, str) or not generation_id:
            raise NelixError(INVALID_REQUEST,
                             f"generation_id must be a non-empty string: {generation_id!r}")
        if not isinstance(state, str) or not state:
            raise NelixError(INVALID_REQUEST,
                             f"state must be a non-empty string: {state!r}")
        cur = self._conn.execute(
            "UPDATE generations SET lifecycle_state=? WHERE generation_id=?",
            (state, generation_id))
        if cur.rowcount == 0:
            raise NelixError(UNKNOWN_SESSION,
                             f"no such generation: {generation_id}")

    @translates_sqlite
    def clear_current_epoch(self, generation_id: str) -> None:
        """Set generations.current_epoch to NULL."""
        if not isinstance(generation_id, str) or not generation_id:
            raise NelixError(INVALID_REQUEST,
                             f"generation_id must be a non-empty string: {generation_id!r}")
        cur = self._conn.execute(
            "UPDATE generations SET current_epoch=NULL WHERE generation_id=?",
            (generation_id,))
        if cur.rowcount == 0:
            raise NelixError(UNKNOWN_SESSION,
                             f"no such generation: {generation_id}")

    @translates_sqlite
    def set_current_epoch(self, generation_id: str, generation_epoch: str) -> None:
        """DIRECT update of generations.current_epoch — used ONLY for repairing
        a dangling NULL pointer to a live serving epoch.  This is NOT a CAS; it is
        a targeted repair.  Callers must verify the epoch is serving first."""
        if not isinstance(generation_id, str) or not generation_id:
            raise NelixError(INVALID_REQUEST,
                             f"generation_id must be a non-empty string: {generation_id!r}")
        if not isinstance(generation_epoch, str) or not generation_epoch:
            raise NelixError(INVALID_REQUEST,
                             f"generation_epoch must be a non-empty string: {generation_epoch!r}")
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE generations SET current_epoch=? WHERE generation_id=?",
                (generation_epoch, generation_id))
        if cur.rowcount == 0:
            raise NelixError(UNKNOWN_SESSION,
                             f"no such generation: {generation_id}")

    @translates_sqlite
    def list_terminal_for_epoch(self, generation_epoch: str) -> list:
        """Owner-agnostic per-epoch terminal enumeration INCLUDING acked/expired.
        Returns list of TerminalRecords sorted by terminal_seq.
        Used by confirmed_high_water resolution (FIX F): resolves EVERY terminal ≤ H
        — board-visible, owner-acked, and validly-expired.
        """
        rows = self._conn.execute(
            "SELECT t.session_id, st.owner_id, st.orchestration_id, "
            "st.generation_id, st.generation_epoch, "
            "t.terminal_kind, t.summary, t.ended_at, t.published_at, t.terminal_seq, "
            "t.acknowledged_at, t.expired_at, t.expire_reason, t.schema_version "
            "FROM terminal t JOIN starts st ON st.session_id = t.session_id "
            "WHERE st.generation_epoch=? ORDER BY t.terminal_seq",
            (generation_epoch,)).fetchall()
        records, _skipped = _read_rows(rows, TerminalRecord)
        return records

    @translates_sqlite
    def set_generation_confirmed_high_water(self, generation_epoch: str, seq: int) -> None:
        """Monotonic advance of generation_progress.confirmed_high_water for an epoch.

        Never regress: only writes if seq > current confirmed_high_water.
        The counter row is created on demand (INSERT OR IGNORE), mirroring
        put_terminal's pattern.
        """
        if not isinstance(generation_epoch, str) or not generation_epoch:
            raise NelixError(INVALID_REQUEST,
                             f"generation_epoch must be a non-empty string: {generation_epoch!r}")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise NelixError(INVALID_REQUEST,
                             f"seq must be a non-negative int: {seq!r}")
        self._conn.execute(
            "INSERT OR IGNORE INTO generation_progress "
            "(generation_id, next_terminal_seq, confirmed_high_water) "
            "VALUES (?, 1, 0)", (generation_epoch,))
        self._conn.execute(
            "UPDATE generation_progress SET confirmed_high_water = MAX("
            "confirmed_high_water, ?) WHERE generation_id=?",
            (seq, generation_epoch))

    @translates_sqlite
    def get_generation_confirmed_high_water(self, generation_epoch: str) -> int:
        """Return the confirmed_high_water for an epoch (0 if none)."""
        if not isinstance(generation_epoch, str) or not generation_epoch:
            raise NelixError(INVALID_REQUEST,
                             f"generation_epoch must be a non-empty string: {generation_epoch!r}")
        row = self._conn.execute(
            "SELECT confirmed_high_water FROM generation_progress WHERE generation_id=?",
            (generation_epoch,)).fetchone()
        return row["confirmed_high_water"] if row else 0

    @translates_sqlite
    def set_generation_lifecycle_state_atomic(self, old_id: str, new_id: str,
                                               new_state_old: str,
                                               expected_old_state: str,
                                               expected_new_state: str) -> None:
        """Atomically transition old generation -> new_state_old AND new generation ->
        active in ONE transaction (both or neither). Raises IDEMPOTENCY_CONFLICT if
        either generation does not exist or is not in the expected state."""
        for gid in (old_id, new_id):
            if not isinstance(gid, str) or not gid:
                raise NelixError(INVALID_REQUEST,
                                 f"generation_id must be a non-empty string: {gid!r}")
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            old_row = self._conn.execute(
                "SELECT lifecycle_state FROM generations WHERE generation_id=?",
                (old_id,)).fetchone()
            if old_row is None:
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 f"no such generation: {old_id}")
            if old_row["lifecycle_state"] != expected_old_state:
                raise NelixError(
                    IDEMPOTENCY_CONFLICT,
                    f"generation {old_id} expected {expected_old_state!r}, "
                    f"actual {old_row['lifecycle_state']!r}")
            new_row = self._conn.execute(
                "SELECT lifecycle_state FROM generations WHERE generation_id=?",
                (new_id,)).fetchone()
            if new_row is None:
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 f"no such generation: {new_id}")
            if new_row["lifecycle_state"] != expected_new_state:
                raise NelixError(
                    IDEMPOTENCY_CONFLICT,
                    f"generation {new_id} expected {expected_new_state!r}, "
                    f"actual {new_row['lifecycle_state']!r}")
            rc1 = self._conn.execute(
                "UPDATE generations SET lifecycle_state=? WHERE generation_id=?",
                (new_state_old, old_id)).rowcount
            rc2 = self._conn.execute(
                "UPDATE generations SET lifecycle_state=? WHERE generation_id=?",
                ("active", new_id)).rowcount
            if rc1 != 1 or rc2 != 1:
                raise NelixError(IDEMPOTENCY_CONFLICT,
                                 "atomic flip: rowcount mismatch")

    @translates_sqlite
    def list_generations(self) -> list:
        """Return ALL generation rows, deterministic order. FAIL-CLOSED on
        any malformed row — does NOT skip unreadable rows (unlike _read_rows
        which is designed for list_sessions/list_terminal's board semantics)."""
        rows = self._conn.execute(
            "SELECT generation_id, build_id, lifecycle_state, current_epoch, "
            "capability_snapshot, created_at "
            "FROM generations ORDER BY created_at, generation_id"
        ).fetchall()
        out = []
        for row in rows:
            out.append(_decode_stored(GenerationRecord, row))
        return out

    @translates_sqlite
    def reconcile_epoch_dead(self, generation_id: str,
                              generation_epoch: str) -> None:
        """One transaction: set this epoch ``dead`` AND clear ``current_epoch``
        ONLY if it still points to this exact epoch.

        Idempotent for an already-dead epoch (returns without error).
        REJECTS a generation/epoch ownership mismatch (epoch belongs to a
        different generation — raises IDEMPOTENCY_CONFLICT).

        If the epoch is already dead but ``current_epoch`` still dangles,
        still clears the dangling pointer (spec §7.7).
        """
        if not isinstance(generation_id, str) or not generation_id:
            raise NelixError(INVALID_REQUEST,
                             f"generation_id must be a non-empty string: {generation_id!r}")
        if not isinstance(generation_epoch, str) or not generation_epoch:
            raise NelixError(INVALID_REQUEST,
                             f"generation_epoch must be a non-empty string: {generation_epoch!r}")
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            # Verify ownership: the epoch belongs to THIS generation.
            ep_row = self._conn.execute(
                "SELECT process_state FROM epochs "
                "WHERE generation_epoch=? AND generation_id=?",
                (generation_epoch, generation_id)).fetchone()
            if ep_row is None:
                raise NelixError(
                    IDEMPOTENCY_CONFLICT,
                    f"epoch {generation_epoch!r} does not belong to generation "
                    f"{generation_id!r}")

            if ep_row["process_state"] != "dead":
                # Mark the epoch dead.
                self._conn.execute(
                    "UPDATE epochs SET process_state='dead' "
                    "WHERE generation_epoch=? AND generation_id=?",
                    (generation_epoch, generation_id))

            # Clear current_epoch ONLY if it still points to this exact epoch.
            # NEVER advance to another epoch; NEVER clear a newer epoch's pointer.
            self._conn.execute(
                "UPDATE generations SET current_epoch=NULL "
                "WHERE generation_id=? AND current_epoch=?",
                (generation_id, generation_epoch))
