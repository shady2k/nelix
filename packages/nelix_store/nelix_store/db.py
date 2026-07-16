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


def connect(root) -> sqlite3.Connection:
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path / DB_FILENAME, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")   # durability: this store's whole point
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    _check_or_stamp_version(conn)
    return conn


def _check_or_stamp_version(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        return
    found = int(row["value"])
    if found > SCHEMA_VERSION:
        # Same fail-closed rule as the record schemas, one level down: an OLDER generation
        # must not open a NEWER generation's database and misread it.
        conn.close()
        raise NelixError(STORE_CORRUPT,
                         f"database schema {found} is newer than this build supports "
                         f"({SCHEMA_VERSION}); refusing to open it")
