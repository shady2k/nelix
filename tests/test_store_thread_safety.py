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
import gc
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


def test_a_fresh_threads_first_read_succeeds_while_another_thread_holds_a_write(tmp_path):
    # nelix-91y review finding #2 (the main functional defect): every thread's FIRST open
    # used to re-run the ENTIRE db bootstrap (flock + BEGIN IMMEDIATE + version stamp + DDL)
    # even once the database was already initialized and already WAL. So a brand-new
    # thread's first call to a READ-ONLY method first attempted this bootstrap WRITE, which
    # collided with an UNRELATED writer's already-open transaction on the SAME shared
    # instance — the WAL-safe SELECT was never reached. timeout=0 makes that collision
    # immediate and certain instead of probabilistic: with busy_timeout=0, any lock
    # contention at all raises SQLITE_BUSY right away.
    #
    # Distinguishes itself from test_write_contention_on_a_shared_store_is_a_retryable_
    # store_unavailable above: that test proves write-vs-write contention correctly
    # surfaces as store_unavailable. This one proves a fresh thread's READ must NOT collide
    # with a concurrent write AT ALL, because get_session/list_sessions never take a write
    # lock themselves — the only thing that could still collide is bootstrap machinery that
    # has no business running once the database is already up.
    store = Store(tmp_path, clock=lambda: 1000.0, timeout=0)   # bootstraps here, main thread
    ledger = StartLedger(tmp_path, clock=lambda: 1000.0)
    r = ledger.reserve(idempotency_key="k1", owner_id=OWNER, orchestration_id=OID,
                       request_fingerprint=FP)
    ledger.assign_generation(r.session_id, GID)
    store.create_session(r.session_id, state="running", executor="coder", task="t",
                         cwd="/repo", model=None, created_at=100.0)
    ledger.close()

    lock_held, release = threading.Event(), threading.Event()

    def holder():
        # This thread's OWN first connection (the instance is already bootstrapped by the
        # main thread above), then an ordinary application write transaction — nothing to
        # do with bootstrap.
        conn = store._conn
        conn.execute("BEGIN IMMEDIATE")
        lock_held.set()
        release.wait(timeout=10)
        conn.execute("ROLLBACK")

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    try:
        assert lock_held.wait(timeout=5), "holder thread never acquired the write lock"

        results = []

        def reader():
            # A FRESH thread: this is its first-ever call to the shared store, made while
            # the holder thread's write transaction is open.
            try:
                results.append(("ok", store.get_session(r.session_id, owner_id=OWNER)))
            except NelixError as e:
                results.append(("error", e))

        rt = threading.Thread(target=reader, daemon=True)
        rt.start()
        rt.join(timeout=10)
        assert not rt.is_alive(), "the fresh reader thread hung"
        assert results, "the fresh reader thread produced no result at all"
        kind, payload = results[0]
        assert kind == "ok", (
            f"a fresh thread's first read failed while another thread held a write: "
            f"{payload.code if isinstance(payload, NelixError) else payload}")
        assert payload.session_id == r.session_id
    finally:
        release.set()
        t.join(timeout=10)
        store.close()


def test_a_long_lived_worker_pool_shares_one_store_across_many_requests_per_worker(tmp_path):
    # The router's actual shape, closer than any other test in this file: a FIXED pool of
    # worker threads, each alive for the process's lifetime and handling MANY requests over
    # time — not the one-shot-per-thread pattern every test above uses. This is the scenario
    # ThreadLocalConnections' lazy-open-once-then-cache-forever path exists to serve: each
    # worker opens its connection ONCE and reuses it across every one of its rounds.
    ledger = StartLedger(tmp_path, clock=lambda: 1000.0)
    store = Store(tmp_path, clock=lambda: 1000.0)

    n_workers, rounds = 4, 25
    errs, lock = [], threading.Lock()

    def worker(worker_id):
        try:
            for round_ in range(rounds):
                r = ledger.reserve(idempotency_key=f"w{worker_id}-r{round_}", owner_id=OWNER,
                                   orchestration_id=OID, request_fingerprint=FP)
                ledger.assign_generation(r.session_id, GID)
                store.create_session(r.session_id, state="running", executor="coder",
                                     task="t", cwd="/repo", model=None, created_at=100.0)
                got = store.get_session(r.session_id, owner_id=OWNER)
                assert got.session_id == r.session_id
        except BaseException as e:          # noqa: BLE001 - account for everything
            with lock:
                errs.append(f"worker {worker_id}, round {round_}: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
              for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    try:
        assert all(not t.is_alive() for t in threads), "a worker hung"
        assert errs == [], f"long-lived worker pool failed: {errs}"
        listed = store.list_sessions(OWNER)
        assert len(listed) == n_workers * rounds, \
            "not every worker's every round made it onto the board"
    finally:
        ledger.close()
        store.close()


def test_many_short_lived_threads_touching_a_shared_store_leave_no_connection_growth(tmp_path):
    # ThreadLocalConnections opens one connection per thread and relies on each thread's OWN
    # teardown to reclaim it (db.py's close() docstring: "measured — 20 sequential threads
    # that each opened a connection and exited without an explicit close left the process's
    # open-fd count unchanged"). That claim had no assertion pinning it anywhere in the suite
    # — a real assertion on live connection count, not a bare pass.
    #
    # Counts LIVE sqlite3.Connection OBJECTS, not raw OS fds: measured, this process's raw
    # `/dev/fd` count is NOT a clean signal here — it also reflects libsqlite3/WAL's own
    # shared-memory bookkeeping on this platform, which does not shrink 1:1 with Python
    # object lifetime even once every `sqlite3.Connection` wrapper is provably gone. Counting
    # the wrapper objects directly is what actually pins THIS package's contract: a
    # short-lived thread's connection must not accumulate in the shared instance.
    #
    # The explicit gc.collect() is required, not decorative: `threading.local`'s per-thread
    # slot is reclaimed through CPython's cyclic collector, not plain refcounting — a dying
    # thread's `_local.conn` reference does not drop the connection's refcount to zero the
    # moment `Thread.join()` returns (verified: without a collection pass, 50 such threads
    # measurably leave their sqlite3.Connection objects alive here). That is not a leak: a
    # real long-running process's normal periodic gen0 collections reclaim it regardless,
    # same as any other reference cycle. Forcing the collection here is what turns
    # "eventually, whenever GC next runs" into a deterministic assertion of the property the
    # docstring claims — no PERMANENT growth, not "reclaimed before the very next line".
    store = Store(tmp_path, clock=lambda: 1000.0)

    def live_connections():
        gc.collect()
        return sum(1 for o in gc.get_objects() if isinstance(o, sqlite3.Connection))

    try:
        before = live_connections()
        for _ in range(50):
            def touch():
                store._conn.execute("SELECT 1").fetchone()

            t = threading.Thread(target=touch)
            t.start()
            t.join(timeout=10)
            assert not t.is_alive(), "a short-lived thread hung"
        after = live_connections()
        assert after <= before, (
            f"live sqlite3.Connection objects grew from {before} to {after} across 50 "
            f"short-lived threads that each opened and dropped their own connection")
    finally:
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
