import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from nelix_contracts import errors
from nelix_contracts.errors import NelixError
from nelix_store import db


def test_connect_creates_the_schema(tmp_path):
    conn = db.connect(tmp_path)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "terminal", "starts", "meta"} <= names


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
    ins = ("INSERT INTO starts (session_id, owner_id, orchestration_id, "
           "idempotency_key, request_fingerprint, state, generation_id, reason, created_at) "
           "VALUES (?,?,?,?,?,?,?,?,?)")
    conn.execute(ins, ("s-1", "o1", "orch", "k1", "fp", "starting", None, None, 1.0))
    # Same owner + same key twice: the DATABASE must refuse, not the application.
    with __import__("pytest").raises(sqlite3.IntegrityError):
        conn.execute(ins, ("s-2", "o1", "orch", "k1", "fp", "starting", None, None, 1.0))
    # A different owner reusing the same key string is a DIFFERENT operation.
    conn.execute(ins, ("s-3", "o2", "orch", "k1", "fp", "starting", None, None, 1.0))


def test_concurrent_first_open_across_processes_never_fails(tmp_path):
    # PROCESSES, not threads: generations are separate processes and the bootstrap lock is
    # inter-process. rev 3's thread-only test could not exercise the mechanism it guards.
    #
    # The gate file is a real barrier — without it, process start-up jitter means they never
    # collide and the test proves nothing.
    root = tmp_path / "store"
    gate = tmp_path / "gate"
    code = (
        "import time, pathlib\n"
        "from nelix_store import db\n"
        f"gate = pathlib.Path({str(gate)!r})\n"
        "while not gate.exists():\n"
        "    time.sleep(0.005)\n"
        f"c = db.connect({str(root)!r})\n"
        "c.close()\n"
        "print('ok')\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(8)]
    try:
        time.sleep(1.0)          # let every process reach the gate
        gate.touch()
        outs = [p.communicate(timeout=60) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
    failures = [f"rc={p.returncode}: {err.strip()}"
                for p, (_out, err) in zip(procs, outs) if p.returncode != 0]
    assert failures == [], f"concurrent first open failed: {failures}"
    assert all(out.strip() == "ok" for out, _err in outs)


def test_concurrent_version_stamping_is_atomic(tmp_path):
    # This test deliberately excludes WAL conversion: the store is bootstrapped ONCE first,
    # so the journal is already converted and the ONLY race left is the meta stamp. rev 3
    # conflated the two, which is why its mutant "failed" 1/10 — it was failing for the WAL
    # race, not for the guard under test, so it proved nothing either way.
    db.connect(tmp_path).close()

    conns = []
    for _ in range(4):
        # check_same_thread=False: each connection is created here, in the main thread, but
        # handed to a DIFFERENT worker thread below. Without this, sqlite3's default
        # thread-affinity check raises ProgrammingError on first use — deterministically,
        # not a race — which proved nothing about the guard under test. (Every other
        # concurrency test in this package has each thread open its OWN connection instead;
        # this test can't, because the barrier proxy must wrap a specific connection object.)
        c = sqlite3.connect(tmp_path / db.DB_FILENAME, isolation_level=None,
                            check_same_thread=False)
        c.row_factory = sqlite3.Row
        conns.append(c)
    conns[0].execute("DELETE FROM meta")

    barrier = threading.Barrier(4)
    errs = []

    def go(conn):
        try:
            db._check_or_stamp_version(_StampBarrier(conn, barrier))
        except BaseException as e:      # noqa: BLE001 - account for everything
            errs.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=go, args=(c,)) for c in conns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    try:
        assert all(not t.is_alive() for t in threads), "a thread hung stamping the version"
        assert errs == [], f"concurrent stamping failed: {errs}"
        row = conns[0].execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert int(row["value"]) == db.SCHEMA_VERSION
    finally:
        for c in conns:
            c.close()


class _StampBarrier:
    """Delegates to a real connection, but pauses every read of the version stamp at a
    barrier BEFORE returning its result.

    This is what makes the race DETERMINISTIC rather than probabilistic. Under rev 2's
    SELECT-then-INSERT, all four callers reach the barrier having each observed "missing",
    are released together, and then all four INSERT — three hit the UNIQUE constraint, every
    run. A correct INSERT OR IGNORE inserts BEFORE it reads, so the barrier changes nothing
    for it.
    """

    def __init__(self, conn, barrier):
        self._conn = conn
        self._barrier = barrier

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        if "SELECT value FROM meta" in sql:
            row = cur.fetchone()
            self._barrier.wait(timeout=20)
            return _OneRow(row)
        return cur


class _OneRow:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


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
