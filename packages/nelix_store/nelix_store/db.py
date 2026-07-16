"""The one SQLite database under NELIX_HOME.

Why SQLite and not JSON files: every hard invariant here is a TRANSACTION — reserve exactly
once under a race, compare-and-set an acknowledgement, create-but-never-overwrite, keep two
writers from clobbering each other. Hand-rolled across files these were wrong in four
separate ways; in a transactional store they are free. sqlite3 is stdlib, so the
stdlib-only constraint still holds.

WAL is on so a reader never blocks a writer — the board is read constantly while
generations write.
"""
import sqlite3
from pathlib import Path

from nelix_contracts.errors import STORE_CORRUPT, NelixError

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

CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    owner_id         TEXT NOT NULL,
    orchestration_id TEXT NOT NULL,
    generation_id    TEXT NOT NULL,
    state            TEXT NOT NULL,
    executor         TEXT NOT NULL,
    task             TEXT NOT NULL,
    cwd              TEXT NOT NULL,
    model            TEXT,
    created_at       REAL NOT NULL,
    schema_version   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_by_owner ON sessions (owner_id);

CREATE TABLE IF NOT EXISTS terminal (
    session_id       TEXT PRIMARY KEY,
    owner_id         TEXT NOT NULL,
    orchestration_id TEXT NOT NULL,
    generation_id    TEXT NOT NULL,
    terminal_kind    TEXT NOT NULL,
    summary          TEXT NOT NULL,
    ended_at         REAL NOT NULL,
    acknowledged_at  REAL,
    schema_version   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS terminal_by_owner ON terminal (owner_id, ended_at);

CREATE TABLE IF NOT EXISTS reservations (
    session_id          TEXT PRIMARY KEY,
    owner_id            TEXT NOT NULL,
    orchestration_id    TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    state               TEXT NOT NULL,
    generation_id       TEXT,
    reason              TEXT,
    created_at          REAL NOT NULL,
    -- The reservation invariant, enforced by the DATABASE rather than by a
    -- check-then-write the application could interleave: one operation per
    -- (owner, key). Two owners reusing the same key STRING are independent.
    UNIQUE (owner_id, idempotency_key)
);
"""


def connect(root, *, timeout: float = 30.0) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise NelixError(STORE_CORRUPT,
                         f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ required "
                         f"(found {sqlite3.sqlite_version})")
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's mode only applies when it CREATES the dir; an existing NELIX_HOME keeps its
    # permissions, and SQLite's -wal/-shm sidecars are created per umask. The directory is
    # the only thing that protects them.
    path.chmod(0o700)
    # timeout is explicit: the default 5s is silent, and WAL conversion on a fresh file needs
    # a brief exclusive lock that concurrent openers must wait for rather than crash on.
    conn = sqlite3.connect(path / DB_FILENAME, isolation_level=None, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")   # durability: this store's whole point
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        _check_or_stamp_version(conn)
    except NelixError:
        conn.close()
        raise
    except sqlite3.Error as e:
        conn.close()
        # No raw sqlite3 exception may cross this boundary: the package's contract is that
        # callers branch on `code`.
        raise NelixError(STORE_CORRUPT, f"could not open the database: {e}") from None
    return conn


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
