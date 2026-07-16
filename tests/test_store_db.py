import sqlite3
import threading

import pytest

from nelix_contracts import errors
from nelix_contracts.errors import NelixError
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


def test_concurrent_first_open_never_leaks_a_raw_sqlite_exception(tmp_path):
    # A reviewer measured 9 failures in 320 concurrent opens of a fresh store: 6 IntegrityError
    # on meta.key, 3 "database is locked". Both escaped raw. An upgrade IS a new generation
    # booting beside the old, so this is the designed topology, not an exotic case.
    barrier = threading.Barrier(8)
    conns, errs = [], []

    def go():
        barrier.wait()
        try:
            conn = db.connect(tmp_path)
            conns.append(conn)
            # sqlite3 connections are thread-affine (stdlib restriction, unrelated to the
            # guard under test): closing from the main thread after join() raises
            # ProgrammingError regardless of db.py's correctness, so each thread closes its
            # own connection here rather than handing it to the joiner.
            conn.close()
        except BaseException as e:          # noqa: BLE001 - the point is to catch EVERYTHING
            errs.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "a thread hung opening the database"
    assert errs == [], f"concurrent open leaked: {errs}"
    assert len(conns) == 8


def test_a_future_database_schema_raises_store_corrupt_specifically(tmp_path):
    # rev 2's test caught bare Exception, so it passed for ValueError or OperationalError too.
    conn = db.connect(tmp_path)
    conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                 (str(db.SCHEMA_VERSION + 1),))
    conn.close()
    with pytest.raises(NelixError) as ei:
        db.connect(tmp_path)
    assert ei.value.code == errors.STORE_CORRUPT


def test_a_malformed_version_stamp_is_store_corrupt_not_a_raw_valueerror(tmp_path):
    conn = db.connect(tmp_path)
    conn.execute("UPDATE meta SET value='banana' WHERE key='schema_version'")
    conn.close()
    with pytest.raises(NelixError) as ei:
        db.connect(tmp_path)
    assert ei.value.code == errors.STORE_CORRUPT


def test_an_older_database_is_refused_rather_than_silently_used(tmp_path):
    # CREATE TABLE IF NOT EXISTS does not add columns to an existing table, so a newer build
    # opening an older file would believe schema N exists while physically using N-1.
    conn = db.connect(tmp_path)
    conn.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
    conn.close()
    with pytest.raises(NelixError) as ei:
        db.connect(tmp_path)
    assert ei.value.code == errors.STORE_CORRUPT


def test_the_sqlite_feature_floor_is_asserted_at_open(tmp_path):
    # prune's ROW_NUMBER() needs SQLite >= 3.25 (2018). The daemon runs a DIFFERENT
    # interpreter than the test venv — that is the exact shape of nelix-cb0, where a
    # 3.13-only API shipped green and broke the real daemon. Assert the floor where it is
    # used, not where it is tested.
    assert sqlite3.sqlite_version_info >= db.MIN_SQLITE
    db.connect(tmp_path).close()
