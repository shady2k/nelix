import errno
import fcntl
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

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


def test_a_brand_new_db_shared_by_many_threads_bootstraps_exactly_once(tmp_path, monkeypatch):
    # nelix-91y review finding #2: ThreadLocalConnections used to delegate the WHOLE
    # per-connection open to connect() unconditionally, so every thread's first use re-ran
    # the entire DB bootstrap (flock + WAL conversion + version stamp + DDL) even once the
    # database was already up. The fix is an INSTANCE-level "bootstrapped" guard: the first
    # connection (from whichever thread gets there first) runs the real bootstrap through
    # connect(), and every other thread's first open must skip it entirely. Counting calls
    # to connect() itself is the only way to observe "exactly once" rather than "idempotent
    # every time", which is the property rev1 already had and is NOT what was asked for.
    calls = []
    real_connect = db.connect

    def counting_connect(*a, **kw):
        calls.append(1)
        return real_connect(*a, **kw)

    monkeypatch.setattr(db, "connect", counting_connect)
    conns = db.ThreadLocalConnections(tmp_path)

    n = 8
    barrier = threading.Barrier(n)
    results, errs = [], []

    def go():
        try:
            barrier.wait(timeout=30)
            conn = conns.get()
            # A real schema table, not `SELECT 1`: `SELECT 1` passes against ANY connection,
            # bootstrapped or not, so it proved nothing about whether the DDL had actually
            # finished before this thread's open returned — an implementation that flipped
            # `_bootstrapped`/released the gate BEFORE the DDL completed would still pass it.
            # `starts` exists only once _SCHEMA has run, so a thread that observes the
            # database before that raises "no such table" here instead of silently reading 1.
            results.append(conn.execute("SELECT count(*) FROM starts").fetchone()[0])
        except BaseException as e:      # noqa: BLE001 - account for everything
            errs.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=go, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(not t.is_alive() for t in threads), "a thread hung on its first open"
    assert errs == [], f"concurrent first opens on a brand-new db failed: {errs}"
    assert results == [0] * n, "not every thread's first open saw a fully-bootstrapped schema"
    assert len(calls) == 1, f"expected exactly ONE full bootstrap, connect() ran {len(calls)}"


def test_a_process_chdir_does_not_change_which_db_file_a_fresh_thread_opens(tmp_path):
    # nelix-91y review finding #3: ThreadLocalConnections used to store the root UNRESOLVED
    # and reinterpret it relative to the process's current directory on every thread's first
    # open. A chdir between two threads' first opens then made ONE shared instance operate on
    # TWO different database files depending on which thread asked and when.
    original_cwd = os.getcwd()
    (tmp_path / "relstore").mkdir()
    os.chdir(tmp_path)
    try:
        conns = db.ThreadLocalConnections("relstore")     # relative to CWD right now
        first_conn = conns.get()                          # constructing thread opens here
        first_path = Path(
            first_conn.execute("PRAGMA database_list").fetchone()["file"]).resolve()

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        os.chdir(elsewhere)

        box = []

        def fresh_thread():
            conn = conns.get()          # this thread's first open, CWD has since changed
            box.append(Path(
                conn.execute("PRAGMA database_list").fetchone()["file"]).resolve())

        t = threading.Thread(target=fresh_thread)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive(), "the fresh thread hung opening its first connection"
        assert box, "the fresh thread's first open failed to produce a connection"
        assert box[0] == first_path, (
            f"a chdir between two threads' first opens changed which db file the SAME "
            f"instance uses: {box[0]} != {first_path}")
    finally:
        os.chdir(original_cwd)


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


def _table_names(tmp_path):
    """Table names read WITHOUT going through db.connect().

    A plain sqlite3.connect() deliberately: connect() is the thing under test, so reading the
    file with it would let the bug hide the bug. NOTE (f1k-db-contract): the plan's version of
    this helper subscripted rows by name (`r["name"]`) on a raw connection, which has no
    row_factory — that raises `TypeError: tuple indices must be integers or slices, not str`
    before any assertion runs, so the test failed for the wrong reason and proved nothing.
    """
    conn = sqlite3.connect(tmp_path / db.DB_FILENAME)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_refusing_an_old_database_does_not_first_mutate_it(tmp_path, monkeypatch):
    # connect() ran the WHOLE schema through executescript() before checking the stored
    # version, so a newer build would create its new tables in an older file and only then
    # refuse to open it — leaving a mutation behind in a file it just called unusable.
    db.connect(tmp_path).close()                       # a real v1 database
    before = _table_names(tmp_path)

    monkeypatch.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION + 1)
    monkeypatch.setattr(db, "_SCHEMA", db._SCHEMA + "\nCREATE TABLE IF NOT EXISTS v2_only (x);")
    with pytest.raises(NelixError) as ei:
        db.connect(tmp_path)
    assert ei.value.code == errors.STORE_CORRUPT

    after = _table_names(tmp_path)
    assert after == before, f"refusing the file still created {after - before}"


def test_an_interrupted_bootstrap_cannot_be_adopted_by_a_newer_build(tmp_path, monkeypatch):
    # The reorder above refuses a STAMPED file before touching it. This is the door it leaves
    # open: a bootstrap that DIES PARTWAY THROUGH ITS DDL, after meta exists but before the
    # stamp is written. A newer build then reads "no stamp" as "a fresh file", applies ITS
    # schema over the older tables and stamps itself — the same half-applied upgrade the
    # version gate exists to prevent, arriving through the back door.
    #
    # The plan's step said to "apply the schema and stamp it in one transaction". Measured:
    # executescript() issues an implicit COMMIT first, so the DDL and the stamp physically
    # CANNOT share a transaction. meta and its stamp are therefore committed atomically FIRST
    # instead, which buys the same property: an interrupted bootstrap always leaves a file
    # that says which version it is.
    #
    # The death is injected here by a failing statement; in the field it is a crash, a SIGKILL
    # or a full disk. Either way SQLite is in autocommit, so the statements that already ran
    # are already committed.
    monkeypatch.setattr(db, "_SCHEMA", db._SCHEMA + "\nTHIS IS NOT VALID SQL;")
    with pytest.raises(NelixError):
        db.connect(tmp_path)
    monkeypatch.undo()
    assert "meta" in _table_names(tmp_path), "the injected death did not get as far as meta"

    # A NEWER build now meets that interrupted file. It must refuse it, not adopt it.
    monkeypatch.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION + 1)
    monkeypatch.setattr(db, "_SCHEMA", db._SCHEMA + "\nCREATE TABLE IF NOT EXISTS v2_only (x);")
    with pytest.raises(NelixError) as ei:
        db.connect(tmp_path)
    assert ei.value.code == errors.STORE_CORRUPT
    assert "v2_only" not in _table_names(tmp_path), \
        "a newer build adopted an interrupted older bootstrap and applied its schema over it"


def test_a_death_between_creating_meta_and_stamping_it_leaves_nothing_behind(tmp_path,
                                                                             monkeypatch):
    # The NARROWEST window, and the one the transaction exists for: meta created, stamp not
    # yet written. Without a transaction around the pair, the file keeps an unstamped meta —
    # and an unstamped meta is exactly what a newer build reads as "a fresh file" and adopts.
    # Committed together, this window has no observable state at all: either the file says
    # which version it is, or it has no meta.
    #
    # Death is injected via a Connection subclass because sqlite3.Connection is a C-extension
    # type whose execute() cannot be patched on the instance (see the WAL test above for the
    # same constraint and the same workaround).
    real_connect = sqlite3.connect

    class _DieOnStamp(sqlite3.Connection):
        def execute(self, sql, params=()):
            if "INSERT OR IGNORE INTO meta" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return super().execute(sql, params)

    def fake_connect(*a, **kw):
        kw.setdefault("factory", _DieOnStamp)
        return real_connect(*a, **kw)

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    with pytest.raises(NelixError):
        db.connect(tmp_path)
    monkeypatch.undo()

    assert "meta" not in _table_names(tmp_path), \
        "a death between creating meta and stamping it left an unstamped meta behind — a " \
        "newer build reads that as a fresh file and applies its schema over it"

    conn = db.connect(tmp_path)       # and the file is still perfectly openable afterwards
    try:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'"
                            ).fetchone()["value"] == str(db.SCHEMA_VERSION)
    finally:
        conn.close()


def test_an_interrupted_bootstrap_is_completed_by_the_same_build(tmp_path, monkeypatch):
    # The other half, and the reason the interrupted file is not simply declared corrupt: the
    # SAME build must be able to finish what it started. Stamping first must not turn a
    # survivable interruption into a permanently unopenable database.
    monkeypatch.setattr(db, "_SCHEMA", db._SCHEMA + "\nTHIS IS NOT VALID SQL;")
    with pytest.raises(NelixError):
        db.connect(tmp_path)
    monkeypatch.undo()

    conn = db.connect(tmp_path)                       # same build, intact schema: must heal
    try:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"sessions", "terminal", "starts", "meta"} <= names
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'"
                            ).fetchone()["value"] == str(db.SCHEMA_VERSION)
    finally:
        conn.close()


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


def test_connect_established_closes_its_new_connection_on_a_pragma_failure(
        tmp_path, monkeypatch):
    # nelix-91y review round 2, finding #4: connect()'s exception path closes the connection
    # it just opened before re-raising (see connect()'s `if conn is not None: conn.close()`
    # arms); _connect_established() did not — a PRAGMA failing AFTER the open succeeded left
    # a live, never-closed sqlite3.Connection behind for every thread whose per-connection
    # setup died partway through. Same factory-subclass interception as the WAL test above,
    # aimed at the LAST pragma _connect_established sets so an earlier one has already
    # succeeded on the leaked connection.
    db.connect(tmp_path).close()        # bootstrap first: _connect_established assumes it

    real_connect = sqlite3.connect
    opened = []

    class _BoomOnForeignKeys(sqlite3.Connection):
        def execute(self, sql, params=()):
            if "foreign_keys" in sql:
                raise sqlite3.OperationalError("simulated pragma failure")
            return super().execute(sql, params)

    def fake_connect(*a, **kw):
        kw.setdefault("factory", _BoomOnForeignKeys)
        conn = real_connect(*a, **kw)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    with pytest.raises(NelixError):
        db._connect_established(tmp_path, timeout=5.0)

    assert opened, "the connection under test was never opened"
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")   # a closed connection refuses any further use


def test_busy_is_unavailable_and_retryable():
    e = sqlite3.OperationalError("database is locked")
    e.sqlite_errorcode = 5           # SQLITE_BUSY
    err = db.classify_sqlite_error(e)
    assert err.code == errors.STORE_UNAVAILABLE
    assert err.retryable is True


def test_a_missing_column_is_our_bug_not_an_infinite_retry():
    # SQLite raises OperationalError for permanent schema defects too. Mapping the CLASS
    # wholesale to retryable means a broken database is retried forever — that is what this
    # test has always been for, and it still holds.
    #
    # What MOVED (nelix-1ul): the party named. This asserted store_corrupt, i.e. "the user's
    # durable data is damaged". A missing column in a schema THIS PACKAGE creates is our DDL
    # failing to match our SQL. Non-retryable either way, so the retry contract below is
    # unchanged; the difference is who gets sent to fix it.
    e = sqlite3.OperationalError("no such column: nope")
    e.sqlite_errorcode = 1           # SQLITE_ERROR
    err = db.classify_sqlite_error(e)
    assert err.code == errors.INTERNAL_ERROR
    assert err.retryable is False


def test_a_corrupt_file_is_corrupt():
    e = sqlite3.DatabaseError("database disk image is malformed")
    e.sqlite_errorcode = 11          # SQLITE_CORRUPT
    assert db.classify_sqlite_error(e).code == errors.STORE_CORRUPT


def test_a_programmer_error_is_not_reported_as_data_corruption(tmp_path):
    # A wrong-thread call, a closed connection and a bad binding are OUR bugs. Reporting them
    # as store_corrupt tells the caller their durable state is damaged — non-retryable, so it
    # escalates to a human for something no human can fix in the data. This is not academic:
    # it is what HID the broken ack/prune seam (nelix-m88), because the seam's prune died of a
    # wrong-thread ProgrammingError and the test could not tell that from a real store failure.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (x)")

    box = []

    def other_thread():
        try:
            conn.execute("SELECT 1")
        except sqlite3.Error as e:
            box.append(e)

    t = threading.Thread(target=other_thread)
    t.start()
    t.join()
    assert box, "the wrong-thread call did not raise"
    assert db.classify_sqlite_error(box[0]).code == errors.INTERNAL_ERROR

    closed = sqlite3.connect(":memory:")
    closed.close()
    try:
        closed.execute("SELECT 1")
    except sqlite3.Error as e:
        assert db.classify_sqlite_error(e).code == errors.INTERNAL_ERROR
    else:
        pytest.fail("a closed connection did not raise")


def test_a_bad_binding_is_not_reported_as_data_corruption(tmp_path):
    # Measured on this interpreter: this one arrives as OperationalError with
    # sqlite_errorcode=1, NOT as a code-less ProgrammingError. A fix keyed only on
    # ProgrammingError leaves it misclassified.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (x)")
    try:
        conn.execute("INSERT INTO t VALUES (?, ?)", (1,))
    except sqlite3.Error as e:
        assert db.classify_sqlite_error(e).code == errors.INTERNAL_ERROR
    else:
        pytest.fail("the bad binding did not raise")


def test_internal_error_is_not_retryable():
    # Retryability is a MACHINE contract: a caller branches on it. No retry of the same call
    # fixes a wrong-thread bug.
    e = db.classify_sqlite_error(sqlite3.ProgrammingError("closed"))
    assert e.code == errors.INTERNAL_ERROR
    assert e.retryable is False


def test_positive_evidence_of_damage_still_names_damage():
    # The negative control for the INTERNAL_ERROR branch: widening "our bug" must not swallow
    # the two codes that are actual proof of a damaged file. SQLite reports those positively;
    # that is the whole basis for no longer inferring damage from silence.
    for code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
        e = sqlite3.DatabaseError("x")
        e.sqlite_errorcode = code
        assert db.classify_sqlite_error(e).code == errors.STORE_CORRUPT, code


def test_public_store_methods_do_not_leak_raw_sqlite_errors(tmp_path, monkeypatch):
    # rev 5 translated only connect(). Every other method leaked raw sqlite3 exceptions under
    # contention — through a package whose contract is "callers branch on code".
    #
    # NOTE (f1k-rev6): the brief's original version of this test did
    # `monkeypatch.setattr(store._conn, "execute", boom)` directly on the live
    # sqlite3.Connection instance. Verified: that raises `AttributeError: 'sqlite3.Connection'
    # object attribute 'execute' is read-only` immediately (same root cause already documented
    # above on `test_a_filesystem_that_cannot_do_wal_is_permanent_not_retryable` — a
    # C-extension type with no instance __dict__).
    #
    # NOTE (nelix-91y): `store._conn` is now a per-thread PROPERTY (it delegates to
    # `store._conns.get()`, ThreadLocalConnections' lazy per-thread opener), so
    # `monkeypatch.setattr(store, "_conn", ...)` no longer works either — a property with no
    # setter is a DATA descriptor, and assigning to it raises `AttributeError: property
    # '_conn' of 'Store' object has no setter` (verified). The interception moves one level
    # further down: `store._conns` is an ordinary Python object and `.get()` an ordinary
    # bound method, so patching `store._conns.get` swaps in the boom proxy for whichever
    # thread calls it — here, the only thread involved. The simulated scenario and
    # assertions are unchanged.
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
        real_conn = store._conn
        monkeypatch.setattr(store._conns, "get", lambda: _BoomConnection(real_conn))
        with pytest.raises(NelixError) as ei:
            store.get_session("s-" + "1" * 32, owner_id="hermes:local")
        assert ei.value.code == errors.STORE_UNAVAILABLE
    finally:
        monkeypatch.undo()
        store.close()


@pytest.mark.parametrize("code,expected", [
    (sqlite3.SQLITE_FULL, errors.STORE_UNAVAILABLE),        # a full disk is not damage
    (sqlite3.SQLITE_NOMEM, errors.STORE_UNAVAILABLE),
    (sqlite3.SQLITE_INTERRUPT, errors.STORE_UNAVAILABLE),
    (sqlite3.SQLITE_BUSY, errors.STORE_UNAVAILABLE),
    (sqlite3.SQLITE_LOCKED, errors.STORE_UNAVAILABLE),
    (sqlite3.SQLITE_READONLY, errors.STORE_UNSUPPORTED),
    (sqlite3.SQLITE_PERM, errors.STORE_UNSUPPORTED),
    (sqlite3.SQLITE_AUTH, errors.STORE_UNSUPPORTED),
    (sqlite3.SQLITE_CORRUPT, errors.STORE_CORRUPT),
    (sqlite3.SQLITE_NOTADB, errors.STORE_CORRUPT),
    # SQLITE_ERROR (1) is generic: malformed SQL, wrong parameter count, missing column, "no
    # such table". This package writes its own SQL and bootstraps its own schema, so all four
    # are OUR defect — it moved off store_corrupt with nelix-1ul.
    (sqlite3.SQLITE_ERROR, errors.INTERNAL_ERROR),
])
def test_sqlite_result_codes_map_to_the_right_party(code, expected):
    # A full disk classified as non-retryable corruption sends the operator to check their
    # data when they should be freeing space.
    e = sqlite3.OperationalError("x")
    e.sqlite_errorcode = code
    assert db.classify_sqlite_error(e).code == expected


def _connect_off_thread(tmp_path, timeout):
    """Run connect() on a thread and report (code, elapsed), or nothing if it never returned.

    Off-thread deliberately: these tests exist to pin a DEADLINE, and a deadline check whose
    deletion turns the loop infinite must surface as a failed assertion, not as a wedged
    pytest that has to be killed.
    """
    out = []

    def go():
        start = time.monotonic()
        try:
            db.connect(tmp_path, timeout=timeout)
            out.append(("returned-without-raising", time.monotonic() - start))
        except NelixError as e:
            out.append((e.code, time.monotonic() - start))

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout=30)
    return out


def test_a_wedged_bootstrap_holder_times_out_within_its_bound(tmp_path):
    # The timeout had NO negative control at all: nothing wedged a holder, so the advertised
    # bound was never once exercised. flock is per open file DESCRIPTION, not per process, so
    # a second fd here contends exactly as another opener's would — no subprocess needed.
    fd = os.open(tmp_path / db.LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        out = _connect_off_thread(tmp_path, 0.3)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert out, "connect() never returned against a wedged holder: the bound is not a bound"
    code, elapsed = out[0]
    assert code == errors.STORE_UNAVAILABLE      # a wedged holder is unavailable, not corrupt
    assert elapsed >= 0.3, "returned before the deadline: it did not wait for the lock"
    assert elapsed < 10, f"overran its {0.3}s bound: {elapsed}s"


def test_a_signal_storm_cannot_spin_past_the_advertised_bound(tmp_path, monkeypatch):
    # Retrying on EINTR is correct — a signal interrupted the syscall. Not rechecking the
    # deadline on that branch is not: a process taking signals continuously would spin past
    # the bound indefinitely. A real signal storm is not deterministic; every flock call
    # failing EINTR is the same branch with none of the timing luck.
    def always_eintr(fd, op):
        raise OSError(errno.EINTR, "interrupted system call")

    monkeypatch.setattr(db.fcntl, "flock", always_eintr)
    out = _connect_off_thread(tmp_path, 0.3)
    assert out, "connect() never returned under continuous EINTR: it spun past its bound"
    code, elapsed = out[0]
    assert code == errors.STORE_UNAVAILABLE
    assert elapsed >= 0.3, "returned before the deadline"
    assert elapsed < 10, f"overran its {0.3}s bound: {elapsed}s"


def test_close_from_a_thread_that_never_touched_the_store_now_succeeds_cleanly(tmp_path):
    # Before nelix-91y, Store() opened its ONE process-wide connection eagerly, in the
    # constructing thread, for the instance's whole life — so close() called from any OTHER
    # thread was a genuine cross-thread misuse: sqlite3 raised ProgrammingError, and
    # (close() being public and undecorated at the time) it leaked raw, then later
    # (decorated) came back as INTERNAL_ERROR. Either way, an operation that should be
    # harmless (closing a store nobody else was using) surfaced as an error.
    #
    # Per-thread connections (ThreadLocalConnections) make the whole class of bug
    # unreachable through the public API: a thread that never touched this Store has no
    # connection of its own to close, so close() has nothing to do here and simply
    # succeeds. This is the positive control for the fix, not just "does not leak raw" —
    # the operation is not merely translated cleanly, it no longer errors at all.
    from nelix_store.store import Store

    store = Store(tmp_path, clock=lambda: 1000.0)
    boom = []

    def closer():
        try:
            store.close()
        except NelixError as e:
            boom.append(e.code)
        except BaseException as e:          # noqa: BLE001
            boom.append(f"RAW {type(e).__name__}")

    t = threading.Thread(target=closer)
    t.start()
    t.join(timeout=10)
    assert boom == [], f"closing from a foreign, never-used thread should be a clean no-op: {boom}"


def test_close_still_translates_a_genuine_sqlite_error(tmp_path):
    # The fix above removes the cross-thread FALSE positive; it must not also remove
    # translation for a TRUE failure closing the calling thread's own connection. Reaches
    # into `store._conns._local` (the ThreadLocalConnections' per-thread slot) to swap in a
    # connection whose close() raises — the same kind of internals-poking the test above
    # this one in the file needed for the same C-extension-immutability reasons.
    from nelix_store.store import Store

    class _BoomOnClose:
        def close(self):
            e = sqlite3.OperationalError("disk I/O error")
            e.sqlite_errorcode = sqlite3.SQLITE_IOERR
            raise e

    store = Store(tmp_path, clock=lambda: 1000.0)
    real_conn = store._conn                        # this thread's real connection, opened
                                                     # eagerly at construction
    store._conns._local.conn = _BoomOnClose()       # swap THIS thread's slot only
    try:
        with pytest.raises(NelixError) as ei:
            store.close()
        assert ei.value.code == errors.STORE_UNAVAILABLE
    finally:
        store._conns._local.conn = None
        real_conn.close()


def test_close_then_reuse_from_the_same_thread_raises_instead_of_silently_reopening(tmp_path):
    # nelix-91y review finding #1: close() only ever dropped the CALLING thread's own
    # cached connection reference (`self._local.conn = None`). The next call from that SAME
    # thread found nothing cached and `get()`'s lazy-open path silently reopened a fresh
    # connection — turning "closed database" into "works again". Before per-thread
    # connections this was impossible to miss: `store.close(); store.list_sessions(...)`
    # raised a stable error. That property must survive: closed means closed, not "reopens
    # on next use".
    from nelix_store.store import Store

    store = Store(tmp_path, clock=lambda: 1000.0)
    store.close()
    with pytest.raises(NelixError) as ei:
        store.list_sessions("hermes:local")
    assert ei.value.code == errors.INTERNAL_ERROR


def test_close_then_use_from_a_fresh_worker_thread_also_raises(tmp_path):
    # The other half of finding #1: a POOL WORKER thread that never touched this Store
    # before its owner called close() must ALSO be refused — not silently granted a brand
    # new connection to an instance its owner already shut down.
    from nelix_store.store import Store

    store = Store(tmp_path, clock=lambda: 1000.0)
    store.close()

    box = []

    def worker():
        try:
            store.get_session("s-" + "1" * 32, owner_id="hermes:local")
        except NelixError as e:
            box.append(e.code)
        except BaseException as e:          # noqa: BLE001
            box.append(f"RAW {type(e).__name__}")

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "the fresh worker thread hung"
    assert box == [errors.INTERNAL_ERROR], (
        f"a fresh worker thread using a closed store should raise a stable closed-instance "
        f"error, got {box}")


def test_close_also_stops_a_worker_thread_that_already_had_its_own_connection_open(tmp_path):
    # The exact scenario the review's bug report names: main constructs the instance, a pool
    # worker opens its OWN connection and keeps it, and main.close() at shutdown physically
    # closes only main's connection (sqlite3 enforces thread affinity on close() just as on
    # execute() — a foreign thread's connection cannot be closed from here; see
    # ThreadLocalConnections.close()'s docstring). The worker's own connection object is
    # therefore still technically open, but the shared _closed flag must still refuse any
    # FURTHER use through the public API — a worker may not go on treating a store its owner
    # already closed as live.
    from nelix_store.store import Store

    store = Store(tmp_path, clock=lambda: 1000.0)
    opened, may_continue, boom = threading.Event(), threading.Event(), []

    def worker():
        try:
            store.get_session("s-" + "1" * 32, owner_id="hermes:local")  # opens ITS OWN conn
        except NelixError:
            pass                          # unknown_session is fine; the open is what matters
        opened.set()
        assert may_continue.wait(timeout=10), "main never signalled after closing"
        try:
            store.get_session("s-" + "1" * 32, owner_id="hermes:local")
        except NelixError as e:
            boom.append(e.code)
        except BaseException as e:          # noqa: BLE001
            boom.append(f"RAW {type(e).__name__}")

    t = threading.Thread(target=worker)
    t.start()
    assert opened.wait(timeout=5), "worker never opened its own connection"
    store.close()
    may_continue.set()
    t.join(timeout=10)
    assert not t.is_alive(), "the worker thread hung"
    assert boom == [errors.INTERNAL_ERROR], (
        f"a worker with an already-open connection kept using a closed store: {boom}")


def test_a_fresh_open_racing_close_raises_instead_of_opening_after_close_returns(
        tmp_path, monkeypatch):
    # nelix-91y review round 2, finding #1: ThreadLocalConnections.get() checked `_closed`
    # and then, on a cache miss, opened a new connection — but the check and the open were
    # NOT atomic. A thread could pass the check, get paused there, watch ANOTHER thread's
    # close() run all the way to completion, and still go on to open and cache a brand-new
    # connection AFTER close() had already returned — exactly what "a closed instance is
    # unusable" forbids.
    #
    # Reproduced deterministically: _open() is patched to pause right where it is entered
    # (before it can take its own lock or touch `_closed`), close() is run to completion
    # during that pause, and only then is the real _open() allowed to proceed. The fix must
    # have the real _open() observe `_closed` and raise WITHOUT ever calling connect() or
    # _connect_established() — i.e. without opening anything.
    conns = db.ThreadLocalConnections(tmp_path)
    conns.get()          # bootstraps on the main thread; this thread keeps its own connection

    real_open = db.ThreadLocalConnections._open
    about_to_open, may_open = threading.Event(), threading.Event()

    def paused_open(self):
        about_to_open.set()
        assert may_open.wait(timeout=10), "close() never ran during the pause"
        return real_open(self)

    monkeypatch.setattr(db.ThreadLocalConnections, "_open", paused_open)

    opened = []
    real_connect, real_established = db.connect, db._connect_established

    def counting_connect(*a, **kw):
        opened.append("connect")
        return real_connect(*a, **kw)

    def counting_established(*a, **kw):
        opened.append("_connect_established")
        return real_established(*a, **kw)

    monkeypatch.setattr(db, "connect", counting_connect)
    monkeypatch.setattr(db, "_connect_established", counting_established)

    results = []

    def fresh_thread():
        try:
            results.append(("ok", conns.get()))
        except NelixError as e:
            results.append(("error", e))

    t = threading.Thread(target=fresh_thread)
    t.start()
    try:
        assert about_to_open.wait(timeout=5), "the fresh thread never reached its first open"
        conns.close()                 # runs to completion WHILE the fresh thread is paused
        may_open.set()
        t.join(timeout=10)
        assert not t.is_alive(), "the fresh thread hung"
        assert results, "the fresh thread produced no result at all"
        kind, payload = results[0]
        assert kind == "error", (
            "a fresh thread's first open raced close() and still opened a connection after "
            "close() had already returned")
        assert payload.code == errors.INTERNAL_ERROR
        assert opened == [], f"a new connection was opened during the race: {opened}"
    finally:
        may_open.set()


# ---- v2->v3 migration correctness (nelix-gm3 round 2) ----

_V2_SCHEMA_TERMINAL = """
CREATE TABLE IF NOT EXISTS terminal (
    session_id      TEXT PRIMARY KEY REFERENCES sessions (session_id) ON DELETE RESTRICT,
    terminal_kind   TEXT NOT NULL,
    summary         TEXT NOT NULL,
    ended_at        REAL NOT NULL,
    published_at    REAL NOT NULL,
    acknowledged_at REAL,
    expired_at      REAL,
    expire_reason   TEXT,
    schema_version  INTEGER NOT NULL,
    CHECK ((expired_at IS NULL) = (expire_reason IS NULL)),
    CHECK (expire_reason IS NULL OR expire_reason IN ('age', 'count')),
    CHECK (expired_at IS NULL OR acknowledged_at IS NULL)
)"""


def _build_v2_database(path):
    """Create a v2 database with populated data across 2 generations. Returns list of expected
    session_ids and per-generation terminal_seq expectations for use by the migration test."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")

    # bootstrap meta with v2 stamp
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '2')")

    # v2 schema (same as v2 _SCHEMA but terminal table without terminal_seq)
    conn.executescript("""
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
    """)
    conn.executescript(_V2_SCHEMA_TERMINAL)
    conn.execute("CREATE INDEX IF NOT EXISTS terminal_by_published ON terminal (published_at)")

    gid1 = "g-" + "a" * 32
    gid2 = "g-" + "b" * 32

    def make_start(sid, owner, gid, key, ts=1.0):
        conn.execute(
            "INSERT INTO starts (session_id, owner_id, orchestration_id, "
            "idempotency_key, request_fingerprint, state, generation_id, reason, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, owner, "o-" + "0" * 32, key, "fp", "starting", gid, None, ts))

    def make_session(sid, state="running", ts=1.0):
        conn.execute(
            "INSERT INTO sessions (session_id, state, executor, task, cwd, model, "
            "created_at, schema_version) VALUES (?,?,?,?,?,?,?,?)",
            (sid, state, "coder", "task", "/repo", None, ts, 2))

    def make_terminal(sid, kind="done", summary="s", ended=5.0, published=1000.0, ts=None):
        conn.execute(
            "INSERT INTO terminal (session_id, terminal_kind, summary, ended_at, "
            "published_at, acknowledged_at, schema_version) VALUES (?,?,?,?,?,?,?)",
            (sid, kind, summary, ended, published, ts, 2))

    # Generation 1: 3 terminals (session ids in insert order)
    sids_g1 = []
    for i in range(3):
        sid = f"s-{i:032x}"
        make_start(sid, "hermes:local", gid1, f"kg1-{i}")
        make_session(sid, ts=10.0 + i)
        make_terminal(sid, ended=100.0 + i, published=2000.0 + i)
        sids_g1.append(sid)

    # Generation 2: 2 terminals
    sids_g2 = []
    for i in range(2):
        sid = f"s-{3+i:032x}"
        make_start(sid, "hermes:local", gid2, f"kg2-{i}")
        make_session(sid, ts=20.0 + i)
        make_terminal(sid, ended=300.0 + i, published=4000.0 + i)
        sids_g2.append(sid)

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return {"gid1": gid1, "gid2": gid2, "sids_g1": sids_g1, "sids_g2": sids_g2}


def test_v2_to_v3_migration_preserves_all_rows_and_backfills_terminal_seq(tmp_path):
    """Build a populated v2 database, migrate to v3 via Store, and verify:
    - list_sessions returns every pre-migration row (none silently dropped)
    - list_terminal returns every pre-migration row (none silently dropped)
    - terminal_seq is backfilled monotonic per generation
    - high-water per generation is correct (>0) and generation_progress continues it
    - may_retire does NOT prematurely retire a generation with durable terminals
    """
    db_path = tmp_path / "nelix.db"
    ctx = _build_v2_database(db_path)

    from nelix_contracts.retirement import may_retire

    # Open a Store — this triggers the v2->v3 migration
    s = db.connect(tmp_path)
    # Verify the stamp was bumped
    stamp = s.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
    assert stamp == str(db.SCHEMA_VERSION), f"stamp stayed {stamp} after migration"

    # ---- (a) list_sessions returns every pre-migration row ----
    store = __import__("nelix_store.store", fromlist=["Store"]).Store
    store_instance = store(tmp_path, clock=lambda: 1000.0)
    try:
        sessions = store_instance.list_sessions("hermes:local")
        sess_ids = {r.session_id for r in sessions}
        expected_sids = set(ctx["sids_g1"] + ctx["sids_g2"])
        missing = expected_sids - sess_ids
        assert not missing, f"list_sessions dropped {len(missing)} rows after migration: {missing}"
        assert len(sessions) == 5, f"expected 5 sessions, got {len(sessions)}"

        # ---- (b) list_terminal returns every pre-migration row ----
        terminals = store_instance.list_terminal("hermes:local")
        term_ids = {r.session_id for r in terminals}
        missing_t = expected_sids - term_ids
        assert not missing_t, f"list_terminal dropped {len(missing_t)} rows after migration: {missing_t}"
        assert len(terminals) == 5, f"expected 5 terminals, got {len(terminals)}"

        # ---- (b) terminal_seq backfilled distinct/monotonic per generation ----
        g1_seqs = [r.terminal_seq for r in terminals
                   if r.session_id in ctx["sids_g1"]]
        g2_seqs = [r.terminal_seq for r in terminals
                   if r.session_id in ctx["sids_g2"]]
        assert g1_seqs == [1, 2, 3], f"gen1 terminal_seq: {g1_seqs}"
        assert g2_seqs == [1, 2], f"gen2 terminal_seq: {g2_seqs}"

        # ---- (c) high-water per generation is correct and generation_progress continues ----
        g1_hw = store_instance.get_generation_persisted_high_water(ctx["gid1"])
        assert g1_hw == 3, f"gen1 high water = {g1_hw}"
        g2_hw = store_instance.get_generation_persisted_high_water(ctx["gid2"])
        assert g2_hw == 2, f"gen2 high water = {g2_hw}"

        # generation_progress.next_terminal_seq continues from the max
        gp1 = s.execute(
            "SELECT next_terminal_seq FROM generation_progress WHERE generation_id=?",
            (ctx["gid1"],)).fetchone()
        assert gp1 is not None, "gen1 missing from generation_progress"
        assert gp1["next_terminal_seq"] == 4, f"gen1 next_seq = {gp1['next_terminal_seq']}"
        gp2 = s.execute(
            "SELECT next_terminal_seq FROM generation_progress WHERE generation_id=?",
            (ctx["gid2"],)).fetchone()
        assert gp2 is not None, "gen2 missing from generation_progress"
        assert gp2["next_terminal_seq"] == 3, f"gen2 next_seq = {gp2['next_terminal_seq']}"

        # ---- (d) may_retire doesn't prematurely retire ----
        # A generation with 3 terminals persisted and only 2 visible to the router
        # must NOT be allowed to retire.
        assert not may_retire(live_pty_count=0, inflight_or_starting_count=0,
                              terminal_persisted_high_water=g1_hw,
                              router_visible_high_water=2), \
            "may_retire returned True when terminals are still unconfirmed"
        # A generation with ALL terminals confirmed CAN retire
        assert may_retire(live_pty_count=0, inflight_or_starting_count=0,
                          terminal_persisted_high_water=g1_hw,
                          router_visible_high_water=3), \
            "may_retire returned False when all terminals are confirmed"
        # A generation with zero terminals is immediately satisfied
        assert may_retire(live_pty_count=0, inflight_or_starting_count=0,
                          terminal_persisted_high_water=0,
                          router_visible_high_water=0), \
            "may_retire returned False for a gen with no terminals"
    finally:
        store_instance.close()
        s.close()
