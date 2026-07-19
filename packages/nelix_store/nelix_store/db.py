"""The one SQLite database under NELIX_HOME.

Why SQLite and not JSON files: every hard invariant here is a TRANSACTION — reserve exactly
once under a race, compare-and-set an acknowledgement, create-but-never-overwrite, keep two
writers from clobbering each other. Hand-rolled across files these were wrong in four
separate ways; in a transactional store they are free. sqlite3 is stdlib, so the
stdlib-only constraint still holds.

WAL is on so a reader never blocks a writer — the board is read constantly while
generations write.
"""
import contextlib
import errno
import fcntl
import functools
import math
import os
import sqlite3
import threading
import time
from pathlib import Path

from nelix_contracts.errors import (
    INTERNAL_ERROR, INVALID_REQUEST, STORE_CORRUPT, STORE_UNAVAILABLE, STORE_UNSUPPORTED,
    NelixError,
)

DB_FILENAME = "nelix.db"
# 4: durable generations/epochs identity tables + v4 migration (nelix-80e-s1a). Moves TOGETHER
#     with records.SCHEMA_VERSION (nelix-165).
SCHEMA_VERSION = 4

# prune_terminal's ROW_NUMBER() window function needs SQLite >= 3.25 (2018). Asserted at
# open because the daemon runs a different interpreter than the test venv — a feature that
# exists in CI and not in production is the nelix-cb0 failure mode.
MIN_SQLITE = (3, 25, 0)

# meta is NOT in _SCHEMA: it is created and stamped together, in one transaction, before the
# rest of the DDL runs. See _stamp_before_the_ddl. It lives here, alone, so the two can never
# drift apart.
_META_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)"""

_SCHEMA = """
-- The ONE authoritative row for a session's identity. Everything else references it.
CREATE TABLE IF NOT EXISTS starts (
    session_id          TEXT PRIMARY KEY,
    owner_id            TEXT NOT NULL,
    orchestration_id    TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    state               TEXT NOT NULL,
    generation_id       TEXT,
    generation_epoch    TEXT,           -- nelix-80e-s1a: per-incarnation epoch (NULL in legacy rows)
    reason              TEXT,
    created_at          REAL NOT NULL,
    UNIQUE (owner_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS starts_by_owner ON starts (owner_id);

-- Live/runtime fields ONLY. Identity comes from starts by join — it is never stored twice,
-- so the two can never disagree.
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY REFERENCES starts (session_id) ON DELETE RESTRICT,
    state          TEXT NOT NULL,
    executor       TEXT NOT NULL,
    task           TEXT NOT NULL,
    cwd            TEXT NOT NULL,
    model          TEXT,
    created_at     REAL NOT NULL,
    schema_version INTEGER NOT NULL
);

-- A PERMANENT RECEIPT, not a payload the pruner may reclaim.
--
-- Terminal idempotency has no key column: its effective key is session_id and its remembered
-- outcome is (terminal_kind, summary, ended_at) IN THIS ROW. Nothing else remembers either the
-- result or the ack — so while prune DELETED this row, the store forgot that the session had
-- ended at all, and the next matching retry inserted a fresh UNACKNOWLEDGED row and put the
-- owner's dismissed result back on their board. The row therefore outlives the board: prune
-- retires it (expired_at) instead of deleting it, and only ever expires rows nobody acked.
--
-- WHY THE SUMMARY IS RETAINED, and not replaced by a digest. Compaction's whole purpose would
-- be to reclaim the payload while keeping equality evidence. Measured on this schema at 1000
-- sessions: dropping a 280-byte summary (daemon/config.py's MAX_SUMMARY_LEN, the only bound
-- this codebase puts on anything called a summary) reclaims 16% of the file — because the
-- sessions.task beside it is far larger, unbounded, retained forever and GC'd by nothing, so
-- compaction reclaims the smaller share of a row that is pinned regardless. A digest would buy
-- that 16% by making SHA-256 over a canonical encoding a permanent durable contract, and by
-- creating a state that cannot exist today: payload and fingerprint, both stored, disagreeing.
-- That is the same objection that rules out a second receipts table, and it does not stop
-- applying because the second representation is a column instead of a table. Equality stays
-- exact. If a writer ever publishes megabyte summaries the arithmetic flips (at 64KB it is
-- 98%) — and put_terminal caps nothing today, so the cheap answer then is a cap at the
-- contract boundary, which bounds task and summary alike, not a digest that bounds one column.
--
-- ended_at is the WORKER's reported fact ("when did I finish"), and it is what the board
-- displays. published_at is the STORE's fact ("when did this become durable here"), and it is
-- the only thing retention may be computed from: prune aged against ended_at, so a worker's own
-- clock decided how long its own result was kept — a stale one reaped it before the owner ever
-- looked, a future one made it immortal. Retention is this package's policy, so it ages from
-- this package's clock.
-- terminal_seq is a per-generation monotonic ordinal assigned by put_terminal, providing the
-- durable, ordered high-water for retirement watermarks (nelix-gm3). Seqs start at 1 per
-- generation and are assigned atomically inside put_terminal's transaction. 0 means unset
-- (legacy row before the column was added, set to 0 by migration; never assigned by current code).
--
-- RECEIPT LIFETIME, written down rather than enforced as a horizon we cannot honour: a receipt
-- lives at least as long as its session and its start. There is no retry horizon and no
-- session/start GC, so receipts cannot be reclaimed independently of either. A future
-- session-history GC deletes start + session + receipt as ONE lifecycle operation, once no
-- replay is legal — which is exactly why both FKs are RESTRICT below.
CREATE TABLE IF NOT EXISTS terminal (
    session_id      TEXT PRIMARY KEY REFERENCES sessions (session_id) ON DELETE RESTRICT,
    terminal_kind   TEXT NOT NULL,
    summary         TEXT NOT NULL,
    ended_at        REAL NOT NULL,
    published_at    REAL NOT NULL,
    terminal_seq    INTEGER NOT NULL DEFAULT 0,
    acknowledged_at REAL,          -- the OWNER dismissed it (their decision, at once)
    expired_at      REAL,          -- the PRUNER retired it (ours, later). Never both.
    expire_reason   TEXT,
    schema_version  INTEGER NOT NULL,
    -- The impossible states, made unrepresentable rather than merely untested. A reader that
    -- cannot receive these needs no branch for them, and SQLite enforces CHECK against every
    -- writer, including one that never goes through this package.
    CHECK ((expired_at IS NULL) = (expire_reason IS NULL)),
    CHECK (expire_reason IS NULL OR expire_reason IN ('age', 'count')),
    -- Dismissal and expiry are mutually exclusive, and this is the constraint the lifecycle
    -- rests on: an acked result is already off the board, so the pruner has no reason to expire
    -- it, and a result the owner never saw cannot have been dismissed. It is also what backstops
    -- ack_terminal's terminal_expired branch — without the branch, the CAS would try to
    -- acknowledge an expired row and SQLite would refuse the write outright.
    CHECK (expired_at IS NULL OR acknowledged_at IS NULL)
);
-- Indexed on published_at, not ended_at: this index exists to serve pruning, and pruning now
-- orders and bounds by published_at.
CREATE INDEX IF NOT EXISTS terminal_by_published ON terminal (published_at);

-- Per-generation monotonic terminal_seq counter. put_terminal atomically increments
-- next_terminal_seq and assigns it as the new terminal's terminal_seq within the SAME
-- transaction, so concurrent put_terminal calls from different sessions of one generation
-- never produce duplicate seqs. Seqs start at 1 (first terminal for a generation gets seq=1).
-- confirmed_high_water is reserved for the router to update in the retirement-ordering slice;
-- gm3 writes only next_terminal_seq.
CREATE TABLE IF NOT EXISTS generation_progress (
    generation_id        TEXT PRIMARY KEY,
    next_terminal_seq    INTEGER NOT NULL DEFAULT 1,
    confirmed_high_water INTEGER NOT NULL DEFAULT 0
);

-- S1a: durable generations/epochs identity (nelix-80e). Circular FK broken by disabling
-- foreign_keys during CREATE: SQLite stores the FK definitions but does not enforce them
-- until foreign_keys is re-enabled, avoiding the chicken-and-egg problem.
PRAGMA foreign_keys=OFF;
CREATE TABLE IF NOT EXISTS epochs (
    generation_epoch  TEXT PRIMARY KEY,
    generation_id     TEXT NOT NULL REFERENCES generations (generation_id) ON DELETE RESTRICT,
    process_state     TEXT NOT NULL,
    retirement_state  TEXT NOT NULL,
    certificate       TEXT,
    final_high_water  INTEGER,
    incarnation_meta  TEXT,
    created_at        REAL NOT NULL,
    UNIQUE (generation_id, generation_epoch),
    CHECK (process_state IN ('starting','serving','dead')),
    CHECK (retirement_state IN ('open','quiescing','certified'))
);
CREATE TABLE IF NOT EXISTS generations (
    generation_id        TEXT PRIMARY KEY,
    build_id             TEXT,
    lifecycle_state      TEXT NOT NULL,
    current_epoch        TEXT,
    capability_snapshot  TEXT,
    created_at           REAL NOT NULL,
    FOREIGN KEY (generation_id, current_epoch)
        REFERENCES epochs (generation_id, generation_epoch) ON DELETE RESTRICT
);
PRAGMA foreign_keys=ON;
CREATE UNIQUE INDEX IF NOT EXISTS epochs_one_serving
    ON epochs (generation_id) WHERE process_state = 'serving';
"""

LOCK_FILENAME = ".db-init.lock"


@contextlib.contextmanager
def _bootstrap_lock(root: Path, timeout: float):
    """Serialize database BOOTSTRAP across processes — never ordinary use.

    `PRAGMA journal_mode=WAL` takes a brief EXCLUSIVE lock to convert the journal of a fresh
    file, and SQLite deliberately does not run the busy handler for some lock upgrades (it
    would risk deadlock) — so no `busy_timeout` value fixes it, as rev 3 proved at ~20-25%
    failure. Checking the mode first is TOCTOU: every opener can see non-WAL before any of
    them converts.

    A bounded NON-blocking flock loop, not a blocking acquire: a wedged holder must surface
    as store_unavailable, not as a hang. The kernel releases the lock if a holder dies, so
    this is crash-safe. Held only across bootstrap, released before the connection is used.

    Only lock CONTENTION (EACCES/EAGAIN — another opener holds it) and EINTR (a signal
    interrupted the syscall; retrying is simply correct) spin to the deadline. Anything else
    — locking not supported on this filesystem, a bad descriptor — is not a condition more
    waiting can fix, so it fails immediately instead of spinning the full timeout for no
    reason.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or \
            not math.isfinite(timeout) or timeout < 0:
        raise NelixError(INVALID_REQUEST,
                         f"timeout must be a finite, non-negative number: {timeout!r}")
    path = root / LOCK_FILENAME
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno == errno.EINTR:
                    # Retrying on EINTR is correct, but not rechecking the deadline made the
                    # advertised bound untrue under a signal storm.
                    if time.monotonic() >= deadline:
                        raise NelixError(STORE_UNAVAILABLE,
                                         f"timed out after {timeout}s waiting for the "
                                         f"database bootstrap lock") from None
                    continue
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise NelixError(
                        STORE_UNSUPPORTED,
                        f"cannot lock the database bootstrap file: {e}") from None
                if time.monotonic() >= deadline:
                    raise NelixError(
                        STORE_UNAVAILABLE,
                        f"timed out after {timeout}s waiting for the database bootstrap lock"
                    ) from None
                time.sleep(0.01)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# Classify by the ERROR CODE, not the exception class: sqlite3 raises OperationalError for
# transient contention AND for permanent schema defects, so mapping the class wholesale to
# retryable makes a missing column retry forever.
#
# Named constants, not magic numbers — and an HONEST policy per primary result code. The old
# comment claimed everything outside these sets proved corruption; a full disk is not damage,
# and telling an operator their data is corrupt when they need to free space is the worst kind
# of wrong answer.
_UNAVAILABLE_CODES = frozenset({
    sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_CANTOPEN,
    sqlite3.SQLITE_IOERR, sqlite3.SQLITE_FULL, sqlite3.SQLITE_NOMEM,
    sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_PROTOCOL,
})
_UNSUPPORTED_CODES = frozenset({
    sqlite3.SQLITE_READONLY, sqlite3.SQLITE_PERM, sqlite3.SQLITE_AUTH,
})
# The ONLY two codes that are positive evidence of a damaged file. Corruption is something
# SQLite reports; it is not something we infer from an error we do not recognise.
#
# HONESTY, measured: deleting this set changes NO classification today, because the fallback
# at the bottom of classify_sqlite_error still returns STORE_CORRUPT and these two codes land
# there anyway. It is a diagnostic (it names damage as damage in the message), not a guard —
# same call as `commit()` at ledger.py:182-186. It is kept because it is the anchor for the
# rule above: if the fallback is ever narrowed, corruption must keep its own positive arm
# rather than silently become an internal error. See the nelix-1ul commit for why the
# fallback was NOT narrowed here.
_CORRUPT_CODES = frozenset({
    sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB,
})
_SQLITE_ERROR = 1


def classify_sqlite_error(exc) -> NelixError:
    """Map a sqlite3 exception onto this package's error contract.

    Retryability is a MACHINE contract: a caller branches on it. Getting it wrong either
    retries a permanent defect forever or escalates a transient one to a human.
    """
    code = getattr(exc, "sqlite_errorcode", None)
    base = None if code is None else code & 0xFF      # strip the extended-code high bits
    if base in _UNAVAILABLE_CODES:
        return NelixError(STORE_UNAVAILABLE, f"database unavailable: {exc}")
    if base in _UNSUPPORTED_CODES:
        return NelixError(STORE_UNSUPPORTED, f"database not writable here: {exc}")
    if base in _CORRUPT_CODES:
        return NelixError(STORE_CORRUPT, f"database corrupt: {exc}")
    # OUR bug, not the caller's data. ProgrammingError/InterfaceError carry no result code at
    # all (measured: wrong thread and closed connection both give sqlite_errorcode=None).
    #
    # SQLITE_ERROR (1) is here too, and deliberately. It is generic — it covers a malformed
    # statement, a wrong parameter count, a missing column AND "no such table" — but every one
    # of those is OUR defect in this package's context, because this package bootstraps its
    # own schema and writes its own SQL: a missing table means our DDL did not run, not that
    # the user's data rotted. (Measured on this interpreter: all four of those raise code 1;
    # a wrong parameter count arrives as OperationalError/1, not as a code-less
    # ProgrammingError, so keying only on the class would leave it misclassified.)
    #
    # A code-less or generic error is not evidence of durable damage — treating it as such
    # reports a code defect as data rot, and sends a human to fix data that is fine.
    if isinstance(exc, (sqlite3.ProgrammingError, sqlite3.InterfaceError)) or base is None \
            or base == _SQLITE_ERROR:
        return NelixError(INTERNAL_ERROR, f"internal database error: {exc}")
    # An unrecognised code keeps the old conservative default, and this is the ONE place the
    # rule above is still not honoured: absence of evidence still names corruption here.
    # Left deliberately, not by oversight. The remaining unrecognised codes are mostly our-bug
    # shapes too (MISUSE, RANGE, MISMATCH, CONSTRAINT), so this arm is probably wrong the same
    # way the code-1 arm was — but reclassifying ~14 result codes is a contract change wider
    # than the defect nelix-1ul describes, and NO test pins any of them (measured: flipping
    # this to INTERNAL_ERROR breaks 0 tests, which means untested, not correct). It wants its
    # own bead and its own evidence, not a silent widening.
    return NelixError(STORE_CORRUPT, f"database error: {exc}")


def translates_sqlite(fn):
    """Wrap a public method so no raw sqlite3 exception can cross the package boundary."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except NelixError:
            raise
        except sqlite3.Error as e:
            raise classify_sqlite_error(e) from None
    return wrapper


def connect(root, *, timeout: float = 30.0) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise NelixError(STORE_UNSUPPORTED,
                         f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ required "
                         f"(found {sqlite3.sqlite_version})")
    path = Path(root)
    conn = None
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir's mode applies only when it CREATES the dir; an existing NELIX_HOME keeps its
        # permissions, and SQLite's -wal/-shm sidecars are created per umask. The directory
        # is the only thing protecting them.
        path.chmod(0o700)
        with _bootstrap_lock(path, timeout):
            # connect() is INSIDE the try: a bad path or a permission failure must not escape
            # raw either (rev 3 left it outside, so its own "no raw sqlite errors" claim was
            # untrue).
            conn = sqlite3.connect(path / DB_FILENAME, isolation_level=None, timeout=timeout)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                actual = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(actual).lower() != "wal":
                    # WAL needs shared-memory + locking semantics a network filesystem does
                    # not provide, and no lock of ours can supply them. nelix is single-host
                    # by design; fail loudly rather than run without durability guarantees.
                    raise NelixError(
                        STORE_UNSUPPORTED,
                        f"could not enable WAL (journal_mode={actual!r}); NELIX_HOME must be "
                        f"on a host-local filesystem")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            # BEFORE any DDL: a file whose stamp disagrees with this build must be refused
            # without being touched. The DDL below is not read-only — it is what makes this
            # ordering load-bearing rather than cosmetic.
            _refuse_a_disagreeing_database(conn)
            # Greenfield — no migration exists. An older-version DB is simply refused
            # by the version gate above and _check_or_stamp_version below.
            _stamp_before_the_ddl(conn)
            conn.executescript(_SCHEMA)
            _check_or_stamp_version(conn)
        return conn
    except NelixError:
        if conn is not None:
            conn.close()
        raise
    except sqlite3.Error as e:
        if conn is not None:
            conn.close()
        raise classify_sqlite_error(e) from None
    except OSError as e:
        if conn is not None:
            conn.close()
        raise NelixError(STORE_UNAVAILABLE, f"could not open the database: {e}") from None


def _connect_established(root: Path, *, timeout: float) -> sqlite3.Connection:
    """Open a PER-THREAD connection to a database THIS INSTANCE has already bootstrapped.

    The counterpart to connect(), for every open after the first (nelix-91y review finding
    #2): connect()'s flock + WAL conversion + version stamp + DDL exist to make an UNKNOWN
    file safe to use, and re-running that WRITE transaction on every thread's first open —
    even once the database is already up and already WAL — collided with an unrelated
    writer's open transaction on the same shared instance, so a brand-new thread's first
    READ could fail with a retryable store_unavailable without ever reaching the WAL-safe
    SELECT that would have succeeded. Once ThreadLocalConnections has bootstrapped the
    database ONCE (see _open), every other thread's connection needs only the per-CONNECTION
    state sqlite3 resets on every new connection object — no flock, no mode check, no
    transaction, no DDL.
    """
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise NelixError(STORE_UNSUPPORTED,
                         f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ required "
                         f"(found {sqlite3.sqlite_version})")
    conn = None
    try:
        conn = sqlite3.connect(root / DB_FILENAME, isolation_level=None, timeout=timeout)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except sqlite3.Error as e:
        # nelix-91y review round 2, finding #4: unlike connect(), this left the connection
        # it had just opened live and unclosed if a PRAGMA after the open raised — matched
        # to connect()'s own `if conn is not None: conn.close()` cleanup so a per-connection
        # setup failure leaks nothing either way.
        if conn is not None:
            conn.close()
        raise classify_sqlite_error(e) from None
    except OSError as e:
        if conn is not None:
            conn.close()
        raise NelixError(STORE_UNAVAILABLE, f"could not open the database: {e}") from None


class ThreadLocalConnections:
    """One sqlite3 connection PER THREAD to the same database file — what makes a single
    Store/StartLedger instance safe to share across concurrent threads (nelix-91y: the
    router is a threaded HTTP server that shares ONE instance across request-handler
    threads).

    Each thread that touches the holder gets its own connection, opened lazily the first
    time THAT thread asks for one — via connect() for the FIRST thread across the whole
    instance (which bootstraps the database), and via the cheap _connect_established() for
    every thread after that (nelix-91y review finding #2: the DB-level bootstrap runs AT
    MOST ONCE per instance, not once per connection — see _open()). `check_same_thread`
    keeps sqlite3's safe default (True) on every one of them — a connection is only ever
    touched by the thread that opened it, so the default is exactly what we want, not an
    obstacle to defeat with `check_same_thread=False`.

    WHY this shape and not the alternatives (the nelix-91y reviewer's call, recorded once
    here rather than re-litigated per call site):
      (a, chosen) per-thread connections over WAL. WAL (already forced by connect()) is
          precisely what lets concurrent readers proceed without blocking a writer — the
          module docstring's whole reason for choosing WAL only pays off if readers and
          writers can actually be concurrent. `BEGIN IMMEDIATE` (already used by every
          writer method in store.py/ledger.py) serializes writers across connections
          through the database's own file lock, and `busy_timeout` (already set by
          connect()) waits out that contention before SQLITE_BUSY can surface — which
          classify_sqlite_error already maps to a retryable STORE_UNAVAILABLE.
      (b, rejected) ONE `check_same_thread=False` connection behind a process-wide lock.
          Throws away WAL's concurrency on purpose — every reader would queue behind the
          lock too — AND a SECOND `BEGIN IMMEDIATE` on the SAME connection while the first
          is still open fails immediately with "cannot start a transaction within a
          transaction" (every writer method here opens one), so the lock guarding it would
          not be an optional optimisation, it would be mandatory just to keep the package
          correct — a strictly worse bottleneck than the concurrency SQLite already gives
          every other multi-connection user for free.
      (c, rejected) a single serialized DB-worker thread. Needless indirection: it
          re-implements, by hand, exactly the serialization WAL + busy_timeout already do
          inside SQLite.
    """

    def __init__(self, root, *, timeout: float = 30.0):
        # Resolved ONCE, here, against the CWD at construction time (nelix-91y review
        # finding #3). The old code stored the root as given and let connect() reinterpret
        # it, via Path(root), on every thread's first open — so a process chdir between two
        # threads' first opens made one shared instance open TWO different files depending
        # on which thread asked and when. An absolute, resolved path has nothing left for a
        # later chdir to change the meaning of.
        self._root = Path(root).resolve()
        self._timeout = timeout
        self._local = threading.local()
        # Guards TWO things, both entered only on a cache miss in get(): "has this INSTANCE
        # bootstrapped its database yet" (nelix-91y review finding #2), and — the SAME
        # lock, not a second one (nelix-91y review round 2, finding #1) — "is a fresh open
        # atomic with close()". Separate from, and in addition to, connect()'s own
        # cross-process flock, which still protects the file itself against a DIFFERENT
        # process's first-open. This one is in-process: the first thread through _open()
        # runs the real connect() and flips `_bootstrapped`; every other thread's first open
        # skips straight to the cheap per-connection-only path — but every one of them,
        # bootstrap or not, takes this SAME lock first, which is what makes the close race
        # below impossible rather than merely unlikely.
        self._open_lock = threading.Lock()
        self._bootstrapped = False
        # A thread-safe flag, not a plain bool: set from whichever thread calls close(),
        # observed by every thread's get() from then on — see close()'s docstring for why
        # this is the whole point of the flag.
        self._closed = threading.Event()

    def get(self) -> sqlite3.Connection:
        """Return THIS thread's connection, opening it on the first call made from this
        thread — and refuse, always, once this instance has been close()d.

        Two different paths check `_closed`, on purpose, at two different strengths:

        FAST path (this thread already has a cached connection): checked here, lockless.
        That connection may have been opened before close() ran, on a different thread, and
        never itself explicitly closed — sqlite3's thread affinity means close() cannot
        reach it; see close()'s docstring — so it must ALSO stop being handed out through
        this API the moment the instance is closed. An in-flight operation racing an
        in-progress close() here is accepted: the connection is already open, and this
        check merely stops FUTURE calls from this thread, not the current one.

        SLOW path (no cached connection yet — a fresh open): NOT checked here. It is
        checked inside _open(), UNDER THE SAME LOCK close() takes to set `_closed` — see
        _open()'s docstring for why a lockless check-then-open here was exactly the race
        nelix-91y review round 2 found: the check and the open were two separate steps, so
        close() could run to completion in the gap between them, and this thread would
        still go on to open and cache a brand-new connection afterwards.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            if self._closed.is_set():
                raise NelixError(INTERNAL_ERROR,
                                 "this store instance is closed and may not be reused")
            return conn
        conn = self._open()
        self._local.conn = conn
        return conn

    def _open(self) -> sqlite3.Connection:
        """Open a NEW connection for the calling thread — refusing instead, atomically,
        if this instance has already been close()d.

        Held for the WHOLE call, not only around the bootstrap branch (nelix-91y review
        round 2, finding #1): the closed-check and the actual open — connect()'s full
        DB-level bootstrap (flock + WAL conversion + version stamp + DDL) for the very
        first thread, or the cheap _connect_established() for every thread after that,
        which must run it AT MOST ONCE per instance — both happen under this ONE lock,
        which close() also takes before it sets `_closed`. That makes the two calls
        mutually exclusive: whichever gets the lock first runs to completion before the
        other can even start, so a fresh open can never land AFTER a close() that has
        already returned — it either finishes first (close() then waits its turn) or never
        starts at all (it sees `_closed` and raises before touching connect() or
        _connect_established()).
        """
        with self._open_lock:
            if self._closed.is_set():
                raise NelixError(INTERNAL_ERROR,
                                 "this store instance is closed and may not be reused")
            if not self._bootstrapped:
                conn = connect(self._root, timeout=self._timeout)
                self._bootstrapped = True
                return conn
            return _connect_established(self._root, timeout=self._timeout)

    def close(self) -> None:
        """Mark this INSTANCE closed for every thread from now on, and close the CALLING
        thread's own connection, if it opened one.

        Takes `_open_lock` just to set `_closed` — see _open()'s docstring for why sharing
        that one lock with every fresh open is what makes the two operations atomic with
        each other, rather than merely usually non-overlapping.

        A per-thread connection can only be closed BY ITS OWN THREAD: sqlite3 enforces
        thread affinity on close() exactly as it does on execute() (measured — a
        foreign-thread `.close()` raises the identical "SQLite objects created in a
        thread can only be used in that same thread" ProgrammingError a foreign-thread
        `.execute()` does). There is no way to reach into another thread's connection and
        close it from here — a worker thread that already opened its own connection keeps
        it, technically open, until ITS OWN thread closes it or exits. That is accepted,
        not fixed: the router's process exit reclaims every fd regardless, and SQLite
        checkpoints WAL back into the main file on a connection's close/exit either way.

        What close() CAN and MUST do instead — the `_closed` flag above — is make every
        SUBSEQUENT use, from ANY thread, refuse rather than silently succeed. Before it,
        close() only ever dropped the calling thread's OWN cached reference, so (a) the
        same thread's next call found nothing cached and quietly opened a fresh connection
        — "closed database" turned into "works again" — and (b) a pool worker that already
        had its own connection open kept right on using the instance, unaware its owner had
        shut it down. Both are exactly what "a closed instance is unusable" must rule out.

        Connections opened by OTHER threads that never re-touch this instance are still
        released when THEIR thread exits: Python drops the thread-local slot's reference as
        part of that thread's own teardown, which runs in that thread, not this one, so the
        connection's ordinary close happens there too (measured: 20 sequential threads that
        each opened a connection and exited without an explicit close left the process's
        live-connection count unchanged, once given a garbage-collection pass — reclaiming
        a `threading.local` slot goes through the cyclic collector, not plain refcounting).
        The router's worker pool is bounded, so the number of per-thread connections ever
        live at once is bounded with it.
        """
        with self._open_lock:
            self._closed.set()
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            conn.close()


def _validate_version(raw) -> None:
    """Judge a stamp that is already in hand. Raises, or returns having agreed.

    Shared by the pre-DDL refusal and the post-DDL stamp-or-verify so the two can never drift
    into disagreeing about what a legal version is.
    """
    try:
        found = int(raw)
    except (TypeError, ValueError):
        raise NelixError(STORE_CORRUPT,
                         f"database version stamp is unreadable: {raw!r}") from None
    if found > SCHEMA_VERSION:
        # An OLDER generation must not open a NEWER generation's database and misread it.
        raise NelixError(STORE_CORRUPT,
                         f"database schema {found} is newer than this build supports "
                         f"({SCHEMA_VERSION}); refusing to open it")
    if found < SCHEMA_VERSION:
        # There is no migration machinery yet, and CREATE TABLE IF NOT EXISTS does not add
        # columns to an existing table — so proceeding would mean believing in a schema the
        # file does not physically have.
        raise NelixError(STORE_CORRUPT,
                         f"database schema {found} predates this build ({SCHEMA_VERSION}) "
                         f"and no migration exists; refusing to open it")


def _refuse_a_disagreeing_database(conn):
    """Refuse an existing database whose stamp disagrees with this build — BEFORE any DDL.

    connect() used to run the whole schema through executescript() and only THEN check the
    stamp, so a newer build opening an older file created its new tables and only afterwards
    declared the file unusable. It left a mutation behind in a file it had just refused, which
    is the one thing a refusal must not do: the operator's rollback to the older build then
    met a file carrying half of the newer schema. The version gate exists precisely because
    CREATE TABLE IF NOT EXISTS cannot alter an existing table; running it first defeated it.

    Reads nothing but the catalogue and one row, and writes nothing at all — a refusal here is
    guaranteed clean because there is nothing yet to roll back.

    Returns the found version string, or None for a fresh/unstamped file — the caller uses
    this to decide whether a migration is needed.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
                    ).fetchone() is None:
        return None          # a fresh file: no stamp can disagree
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        # meta exists but was never stamped: a bootstrap that died between its DDL and its
        # stamp. Not a disagreement — let the DDL below complete it.
        return None
    raw = row["value"]
    try:
        found = int(raw)
    except (TypeError, ValueError):
        raise NelixError(STORE_CORRUPT,
                         f"database version stamp is unreadable: {raw!r}") from None
    if found > SCHEMA_VERSION:
        raise NelixError(STORE_CORRUPT,
                         f"database schema {found} is newer than this build supports "
                         f"({SCHEMA_VERSION}); refusing to open it")
    if found == SCHEMA_VERSION:
        return raw            # same version: ready to go
    # Greenfield — no migration exists. Refuse immediately without touching DDL.
    raise NelixError(STORE_CORRUPT,
                     f"database schema {found} predates this build "
                     f"({SCHEMA_VERSION}) and no migration exists; refusing to open it")

def _stamp_before_the_ddl(conn):
    """Create meta and write the stamp in ONE transaction, BEFORE the rest of the DDL.

    The plan for this change said to "apply the schema and stamp it in one transaction".
    Measured: `executescript()` issues an implicit COMMIT before it runs, so the DDL and the
    stamp physically cannot share a transaction — the literal instruction is impossible. This
    buys the property it was after, from the other end.

    WHY IT MATTERS that the stamp goes FIRST, not last. A bootstrap can die partway through
    its DDL (a crash, a SIGKILL, a full disk), and SQLite is in autocommit here, so whatever
    already ran is already durable. With the stamp written LAST, such a file has meta but no
    stamp — and a NEWER build reads "no stamp" as "a fresh file", applies its own schema over
    the older tables and stamps itself. That is precisely the half-applied upgrade the version
    gate exists to prevent, walking in through the door the reordering above left open
    (measured: a v2 build adopted an interrupted v1 file and stamped it v2).

    Stamped first, an interrupted bootstrap always leaves a file that SAYS which version it
    is: the same build finishes it (every statement here and in _SCHEMA is idempotent), and a
    newer build refuses it — cleanly, above, before touching it.

    This is deliberately NOT a reason to reject an unstamped meta as corrupt: an interrupted
    first bootstrap must stay completable, not become permanently unopenable.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_META_DDL)
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _check_or_stamp_version(conn):
    """Stamp-or-verify in ONE atomic step.

    rev 2 did SELECT-then-INSERT with no transaction, so eight concurrent first-opens raced:
    6/320 hit `UNIQUE constraint failed: meta.key`, 3/320 hit `database is locked`. That is
    the very check-then-write class this store moved to SQLite to abolish — reintroduced one
    layer underneath the code that abolished it. INSERT OR IGNORE makes the database the
    arbiter, exactly like reservations' UNIQUE constraint.

    Still the authority, and still atomic, even though _refuse_a_disagreeing_database has
    usually read the stamp already: that read is a fast refusal, NOT a check whose result this
    function may trust. Demoting this to a plain verify would turn stamp-or-verify back into
    the check-then-write it exists to abolish.
    """
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    _validate_version(row["value"])
