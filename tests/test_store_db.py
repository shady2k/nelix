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
    # PROCESSES, not threads: generations are separate processes and the lock is
    # inter-process.
    #
    # TWO things make this a real negative control, both learned the hard way:
    #   * a REAL barrier, not a latch — each child publishes a ready marker and the parent
    #     waits for all of them. The previous version slept 1s and ASSUMED.
    #   * REPETITION over fresh directories — the collision window is narrow, so one round
    #     caught a removed lock only ~40% of the time. More processes does not help (8/16/32
    #     measured, no trend); more rounds does.
    rounds = 10
    for attempt in range(rounds):
        root = tmp_path / f"store{attempt}"
        gate = tmp_path / f"gate{attempt}"
        ready = tmp_path / f"ready{attempt}"
        ready.mkdir()
        code = (
            "import os, sys, time, pathlib\n"
            "from nelix_store import db\n"
            f"pathlib.Path({str(ready)!r}, str(os.getpid())).touch()\n"
            f"gate = pathlib.Path({str(gate)!r})\n"
            "while not gate.exists():\n"
            "    time.sleep(0.002)\n"
            f"c = db.connect({str(root)!r})\n"
            "c.close()\n"
            "print('ok')\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(8)]
        try:
            deadline = time.monotonic() + 30
            while len(list(ready.iterdir())) < 8:
                assert time.monotonic() < deadline, "children never reported ready"
                time.sleep(0.01)
            gate.touch()                       # a real barrier: all eight are at the line
            outs = [p.communicate(timeout=60) for p in procs]
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
        failures = [f"round {attempt} rc={p.returncode}: {err.strip()}"
                    for p, (_out, err) in zip(procs, outs) if p.returncode != 0]
        assert failures == [], f"concurrent first open failed: {failures}"


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


def test_an_unsupported_sqlite_is_permanent_not_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 24, 0))
    with pytest.raises(NelixError) as ei:
        db.connect(tmp_path)
    assert ei.value.code == errors.STORE_UNSUPPORTED
    assert ei.value.retryable is False


def test_a_filesystem_that_cannot_do_wal_is_permanent_not_retryable(tmp_path, monkeypatch):
    # Simulates a network NELIX_HOME: WAL needs shared-memory + locking semantics NFS/SMB do
    # not provide, and no lock of ours can supply them.
    #
    # NOTE (f1k-rev5): the brief's original version of this test assigned `conn.execute =
    # execute` on a live sqlite3.Connection instance. sqlite3.Connection is a C-extension
    # type with no instance __dict__ for its methods, so that raises
    # `AttributeError: 'sqlite3.Connection' object attribute 'execute' is read-only` before
    # db.connect() is ever reached — the test cannot run as given. A Connection SUBCLASS can
    # override execute() (verified), so the interception moves there via factory=; the
    # simulated scenario and assertions are unchanged.
    real_connect = sqlite3.connect

    class _FakeWALConnection(sqlite3.Connection):
        def execute(self, sql, params=()):
            if "journal_mode" in sql:
                class _R:
                    def fetchone(self_inner):
                        return ("delete",)      # the conversion silently did not take
                return _R()
            return super().execute(sql, params)

    def fake_connect(*a, **kw):
        kw.setdefault("factory", _FakeWALConnection)
        return real_connect(*a, **kw)

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    with pytest.raises(NelixError) as ei:
        db.connect(tmp_path)
    assert ei.value.code == errors.STORE_UNSUPPORTED


def test_busy_is_unavailable_and_retryable():
    e = sqlite3.OperationalError("database is locked")
    e.sqlite_errorcode = 5           # SQLITE_BUSY
    err = db.classify_sqlite_error(e)
    assert err.code == errors.STORE_UNAVAILABLE
    assert err.retryable is True


def test_a_missing_column_is_corrupt_not_an_infinite_retry():
    # SQLite raises OperationalError for permanent schema defects too. Mapping the CLASS
    # wholesale to retryable means a broken database is retried forever.
    e = sqlite3.OperationalError("no such column: nope")
    e.sqlite_errorcode = 1           # SQLITE_ERROR
    err = db.classify_sqlite_error(e)
    assert err.code == errors.STORE_CORRUPT
    assert err.retryable is False


def test_a_corrupt_file_is_corrupt():
    e = sqlite3.DatabaseError("database disk image is malformed")
    e.sqlite_errorcode = 11          # SQLITE_CORRUPT
    assert db.classify_sqlite_error(e).code == errors.STORE_CORRUPT


def test_public_store_methods_do_not_leak_raw_sqlite_errors(tmp_path, monkeypatch):
    # rev 5 translated only connect(). Every other method leaked raw sqlite3 exceptions under
    # contention — through a package whose contract is "callers branch on code".
    #
    # NOTE (f1k-rev6): the brief's original version of this test did
    # `monkeypatch.setattr(store._conn, "execute", boom)` directly on the live
    # sqlite3.Connection instance. Verified: that raises `AttributeError: 'sqlite3.Connection'
    # object attribute 'execute' is read-only` immediately (same root cause already documented
    # above on `test_a_filesystem_that_cannot_do_wal_is_permanent_not_retryable` — a
    # C-extension type with no instance __dict__) — and unlike that test, patching cannot move
    # to a Connection subclass via `factory=` here, because `store._conn` is already
    # constructed by the time this test runs; `sqlite3.Connection.execute = ...` at the class
    # level also fails (`TypeError: cannot set 'execute' attribute of immutable type
    # 'sqlite3.Connection'`, verified). The interception moves one level up instead: `Store` is
    # an ordinary Python object, so `monkeypatch.setattr(store, "_conn", ...)` swaps in a thin
    # proxy that raises on execute() and delegates everything else to the real connection. The
    # simulated scenario and assertions are unchanged.
    from nelix_store.store import Store

    class _BoomConnection:
        def __init__(self, real):
            self._real = real

        def execute(self, *a, **k):
            e = sqlite3.OperationalError("database is locked")
            e.sqlite_errorcode = 5
            raise e

        def __getattr__(self, name):
            return getattr(self._real, name)

    store = Store(tmp_path, clock=lambda: 1000.0)
    try:
        monkeypatch.setattr(store, "_conn", _BoomConnection(store._conn))
        with pytest.raises(NelixError) as ei:
            store.get_session("s-" + "1" * 32, owner_id="hermes:local")
        assert ei.value.code == errors.STORE_UNAVAILABLE
    finally:
        monkeypatch.undo()
        store.close()
