import sqlite3

from nelix_store import db


def test_connect_creates_the_schema(tmp_path):
    conn = db.connect(tmp_path)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "terminal", "reservations", "meta"} <= names


def test_rows_come_back_addressable_by_column_name(tmp_path):
    conn = db.connect(tmp_path)
    row = conn.execute("SELECT 1 AS answer").fetchone()
    assert row["answer"] == 1


def test_wal_is_on_so_a_reader_never_blocks_a_writer(tmp_path):
    conn = db.connect(tmp_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_reopening_an_existing_database_is_idempotent(tmp_path):
    db.connect(tmp_path).close()
    conn = db.connect(tmp_path)
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'"
                        ).fetchone()["value"] == str(db.SCHEMA_VERSION)


def test_a_future_database_schema_fails_closed(tmp_path):
    conn = db.connect(tmp_path)
    conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                 (str(db.SCHEMA_VERSION + 1),))
    conn.commit()
    conn.close()
    # An OLDER generation must not open a NEWER generation's database and misread it — the
    # same fail-closed rule as the record schemas, one level down.
    with __import__("pytest").raises(Exception) as ei:
        db.connect(tmp_path)
    assert "schema" in str(ei.value).lower()


def test_the_reservation_key_is_unique_per_owner(tmp_path):
    conn = db.connect(tmp_path)
    ins = ("INSERT INTO reservations (session_id, owner_id, orchestration_id, "
           "idempotency_key, request_fingerprint, state, generation_id, reason, created_at) "
           "VALUES (?,?,?,?,?,?,?,?,?)")
    conn.execute(ins, ("s-1", "o1", "orch", "k1", "fp", "starting", None, None, 1.0))
    # Same owner + same key twice: the DATABASE must refuse, not the application.
    with __import__("pytest").raises(sqlite3.IntegrityError):
        conn.execute(ins, ("s-2", "o1", "orch", "k1", "fp", "starting", None, None, 1.0))
    # A different owner reusing the same key string is a DIFFERENT operation.
    conn.execute(ins, ("s-3", "o2", "orch", "k1", "fp", "starting", None, None, 1.0))
