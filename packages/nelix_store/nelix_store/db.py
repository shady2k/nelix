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
import fcntl
import os
import sqlite3
import time
from pathlib import Path

from nelix_contracts.errors import STORE_CORRUPT, STORE_UNAVAILABLE, NelixError

DB_FILENAME = "nelix.db"
SCHEMA_VERSION = 1

# prune_terminal's ROW_NUMBER() window function needs SQLite >= 3.25 (2018). Asserted at
# open because the daemon runs a different interpreter than the test venv — a feature that
# exists in CI and not in production is the nelix-cb0 failure mode.
MIN_SQLITE = (3, 25, 0)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The ONE authoritative row for a session's identity. Everything else references it.
CREATE TABLE IF NOT EXISTS starts (
    session_id          TEXT PRIMARY KEY,
    owner_id            TEXT NOT NULL,
    orchestration_id    TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    state               TEXT NOT NULL,
    generation_id       TEXT,
    reason              TEXT,
    created_at          REAL NOT NULL,
    UNIQUE (owner_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS starts_by_owner ON starts (owner_id);

-- Live/runtime fields ONLY. Identity comes from starts by join — it is never stored twice,
-- so the two can never disagree.
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY REFERENCES starts (session_id),
    state          TEXT NOT NULL,
    executor       TEXT NOT NULL,
    task           TEXT NOT NULL,
    cwd            TEXT NOT NULL,
    model          TEXT,
    created_at     REAL NOT NULL,
    schema_version INTEGER NOT NULL
);

-- Terminal-result fields ONLY.
CREATE TABLE IF NOT EXISTS terminal (
    session_id      TEXT PRIMARY KEY REFERENCES sessions (session_id),
    terminal_kind   TEXT NOT NULL,
    summary         TEXT NOT NULL,
    ended_at        REAL NOT NULL,
    acknowledged_at REAL,
    schema_version  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS terminal_by_ended ON terminal (ended_at);
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
    """
    path = root / LOCK_FILENAME
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
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


def connect(root, *, timeout: float = 30.0) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise NelixError(STORE_UNAVAILABLE,
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
                        STORE_UNAVAILABLE,
                        f"could not enable WAL (journal_mode={actual!r}); NELIX_HOME must be "
                        f"on a host-local filesystem")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            _check_or_stamp_version(conn)
        return conn
    except NelixError:
        if conn is not None:
            conn.close()
        raise
    except sqlite3.OperationalError as e:
        if conn is not None:
            conn.close()
        # Busy / locked / cannot-open: the store is UNAVAILABLE, not damaged. Non-retryable
        # STORE_CORRUPT here would send a caller to a human for a condition that clears.
        raise NelixError(STORE_UNAVAILABLE, f"database unavailable: {e}") from None
    except sqlite3.Error as e:
        if conn is not None:
            conn.close()
        raise NelixError(STORE_CORRUPT, f"could not open the database: {e}") from None
    except OSError as e:
        if conn is not None:
            conn.close()
        raise NelixError(STORE_UNAVAILABLE, f"could not open the database: {e}") from None


def _check_or_stamp_version(conn):
    """Stamp-or-verify in ONE atomic step.

    rev 2 did SELECT-then-INSERT with no transaction, so eight concurrent first-opens raced:
    6/320 hit `UNIQUE constraint failed: meta.key`, 3/320 hit `database is locked`. That is
    the very check-then-write class this store moved to SQLite to abolish — reintroduced one
    layer underneath the code that abolished it. INSERT OR IGNORE makes the database the
    arbiter, exactly like reservations' UNIQUE constraint.
    """
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    try:
        found = int(row["value"])
    except (TypeError, ValueError):
        raise NelixError(STORE_CORRUPT,
                         f"database version stamp is unreadable: {row['value']!r}") from None
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
