"""ONE Store / ONE StartLedger instance, shared across concurrent threads (nelix-91y).

Every concurrency test elsewhere in this package sidesteps the defect this file is FOR:
each thread there constructs its OWN fresh Store/StartLedger (see test_store_ledger.py's
test_exactly_one_reservation_survives_a_race and test_store_db.py's
test_concurrent_version_stamping_is_atomic). That is a different, also-real property
("concurrent first-open is safe"), but it says nothing about the router's actual shape: ONE
instance, constructed once, handed to N request-handler threads. Before ThreadLocalConnections
(db.py), the FIRST time a second thread touched a shared instance's single process-wide
connection, sqlite3 raised `ProgrammingError: SQLite objects created in a thread can only be
used in that same thread` — mapped to INTERNAL_ERROR, a 5xx for what is purely this package's
own connection-plumbing bug, not the caller's.
"""
import sqlite3
import threading

import pytest

from nelix_contracts import errors
from nelix_contracts.errors import NelixError
from nelix_store.ledger import StartLedger
from nelix_store.store import Store

OWNER = "hermes:local"
OID = "o-" + "2" * 32
GID = "g-" + "3" * 32
FP = "fingerprint-of-the-start-request"
N = 8


def test_reserve_from_many_threads_sharing_one_ledger_all_succeed_distinctly(tmp_path):
    # THE test every other concurrency test in this package avoids: ONE StartLedger,
    # touched by N threads that never constructed it. Distinct idempotency keys, so
    # nothing here is ALSO exercising the (already-covered-elsewhere) UNIQUE-constraint
    # race — the only new variable is sharing one instance.
    ledger = StartLedger(tmp_path, clock=lambda: 1000.0)

    results, errs, barrier = [], [], threading.Barrier(N)

    def go(i):
        try:
            barrier.wait(timeout=30)
            r = ledger.reserve(idempotency_key=f"k{i}", owner_id=OWNER,
                               orchestration_id=OID, request_fingerprint=FP)
            results.append(r.session_id)
        except BaseException as e:          # noqa: BLE001 - account for EVERYTHING
            errs.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=go, args=(i,), daemon=True) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    try:
        assert all(not t.is_alive() for t in threads), "a thread hung"
        assert errs == [], f"sharing one ledger across threads failed: {errs}"
        assert len(results) == N, "a thread vanished without a result or an error"
        assert len(set(results)) == N, "distinct keys minted colliding session ids"

        rows = ledger._conn.execute("SELECT COUNT(*) FROM starts").fetchone()[0]
        assert rows == N, f"expected {N} starts rows, found {rows}"
    finally:
        ledger.close()


def test_create_session_from_many_threads_sharing_one_store_all_succeed(tmp_path):
    # The Store half of the same property. Each thread creates and then immediately reads
    # back a DIFFERENT session on the ONE shared instance — no thread ever constructed it.
    #
    # create_session requires an existing, assigned `starts` row (identity is joined from
    # it, never stored twice), so a throwaway StartLedger mints one per thread up front,
    # sequentially, on the main thread — the concurrency under test belongs to Store alone.
    ledger = StartLedger(tmp_path, clock=lambda: 1000.0)
    reserved = []
    for i in range(N):
        r = ledger.reserve(idempotency_key=f"k{i}", owner_id=OWNER, orchestration_id=OID,
                           request_fingerprint=FP)
        ledger.assign_generation(r.session_id, GID)
        reserved.append(r.session_id)
    ledger.close()

    store = Store(tmp_path, clock=lambda: 1000.0)
    errs, barrier = [], threading.Barrier(N)

    def go(sid):
        try:
            barrier.wait(timeout=30)
            store.create_session(sid, state="running", executor="coder", task="t",
                                 cwd="/repo", model=None, created_at=100.0)
            got = store.get_session(sid, owner_id=OWNER)
            assert got.session_id == sid
        except BaseException as e:          # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=go, args=(sid,), daemon=True) for sid in reserved]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    try:
        assert all(not t.is_alive() for t in threads), "a thread hung"
        assert errs == [], f"sharing one store across threads failed: {errs}"
        listed = {s.session_id for s in store.list_sessions(OWNER)}
        assert listed == set(reserved), "not every thread's session made it onto the board"
    finally:
        store.close()


def test_write_contention_on_a_shared_store_is_a_retryable_store_unavailable(tmp_path):
    # Forces the real SQLITE_BUSY path (not just a unit test of classify_sqlite_error): a
    # tiny busy_timeout plus a holder thread that keeps the write lock open longer than
    # that timeout. The point under test is that the error crossing the public API is a
    # retryable NelixError(STORE_UNAVAILABLE), never a raw sqlite3.OperationalError.
    ledger = StartLedger(tmp_path, clock=lambda: 1000.0)
    r = ledger.reserve(idempotency_key="k1", owner_id=OWNER, orchestration_id=OID,
                       request_fingerprint=FP)
    ledger.assign_generation(r.session_id, GID)
    ledger.close()

    store = Store(tmp_path, clock=lambda: 1000.0, timeout=0.2)   # 200ms busy_timeout
    lock_held, release = threading.Event(), threading.Event()

    def holder():
        # store._conn: THIS thread's own lazily-opened connection to the same file.
        conn = store._conn
        conn.execute("BEGIN IMMEDIATE")
        lock_held.set()
        release.wait(timeout=10)
        conn.execute("ROLLBACK")

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    try:
        assert lock_held.wait(timeout=5), "holder thread never acquired the write lock"
        with pytest.raises(NelixError) as ei:
            store.create_session(r.session_id, state="running", executor="coder", task="t",
                                 cwd="/repo", model=None, created_at=100.0)
        assert ei.value.code == errors.STORE_UNAVAILABLE, \
            f"expected store_unavailable under lock contention, got {ei.value.code}"
        assert ei.value.retryable is True
    finally:
        release.set()
        t.join(timeout=10)
        store.close()


def test_foreign_keys_are_enforced_on_a_connection_opened_by_a_non_creating_thread(tmp_path):
    # foreign_keys=ON is a PER-CONNECTION pragma (db.py's connect()), not a persistent
    # database property — unlike journal_mode=WAL. A connection this package opens LAZILY
    # for a worker thread must therefore have it set just as surely as the constructing
    # thread's connection does; nothing may rely on the constructing thread's pragma
    # somehow covering a connection it never touched. Bypasses create_session's own
    # UNKNOWN_SESSION app-level guard (which would otherwise catch this first) by writing
    # the raw INSERT directly, so what actually raises is the DATABASE refusing the
    # dangling foreign key — proof the pragma, not just the app guard, is live.
    store = Store(tmp_path, clock=lambda: 1000.0)
    box = []

    def worker():
        try:
            store._conn.execute(
                "INSERT INTO sessions (session_id, state, executor, task, cwd, model, "
                "created_at, schema_version) VALUES (?,?,?,?,?,?,?,?)",
                ("s-" + "9" * 32, "running", "coder", "t", "/repo", None, 1.0, 2))
        except sqlite3.IntegrityError as e:
            box.append(str(e))
        except BaseException as e:          # noqa: BLE001
            box.append(f"WRONG {type(e).__name__}: {e}")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=10)
    try:
        assert box, "an FK violation on a worker-thread connection did not raise at all"
        assert "FOREIGN KEY" in box[0], f"wrong failure mode: {box[0]}"
    finally:
        store.close()
