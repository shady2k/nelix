"""Tests for the S1a generations/epochs identity tables and API (nelix-80e-s1a).

Real Store on a temp dir, injected clock — never mocks. Follows the house style of
test_store_db.py and test_store_terminal.py.
"""
import sqlite3

import pytest

from nelix_contracts.errors import (
    DUPLICATE_START, IDEMPOTENCY_CONFLICT, INVALID_REQUEST, UNKNOWN_SESSION,
    NelixError,
)
from nelix_contracts.records import SCHEMA_VERSION
from nelix_store import db
from nelix_store.store import Store


def _store(tmp_path, clock=lambda: 1000.0):
    s = Store(tmp_path, clock=clock)
    return s


# ---- Fresh-DB schema ----

def test_fresh_db_has_generations_and_epochs_tables(tmp_path):
    conn = db.connect(tmp_path)
    try:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"generations", "epochs"} <= names
        idx = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "epochs_one_serving" in idx
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(starts)")}
        assert "generation_epoch" in cols
    finally:
        conn.close()


def test_foreign_keys_are_on_after_ddl(tmp_path):
    conn = db.connect(tmp_path)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_generation_epoch_insert_sequence_under_foreign_keys_on(tmp_path):
    """A generation→epoch→current_epoch insert sequence succeeds under FKs=ON."""
    conn = db.connect(tmp_path)
    try:
        gen_id = "g-11111111111111111111111111111111"
        epoch = "g-22222222222222222222222222222222"
        conn.execute(
            "INSERT INTO generations (generation_id, build_id, lifecycle_state, "
            "current_epoch, capability_snapshot, created_at) VALUES (?,?,?,?,?,?)",
            (gen_id, "b1", "active", None, None, 1000.0))
        conn.execute(
            "INSERT INTO epochs (generation_epoch, generation_id, process_state, "
            "retirement_state, certificate, final_high_water, incarnation_meta, "
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (epoch, gen_id, "serving", "open", None, None, None, 1000.0))
        conn.execute(
            "UPDATE generations SET current_epoch=? WHERE generation_id=?",
            (epoch, gen_id))
        conn.commit()
        row = conn.execute(
            "SELECT current_epoch FROM generations WHERE generation_id=?",
            (gen_id,)).fetchone()
        assert row["current_epoch"] == epoch
    finally:
        conn.close()


def test_serving_partial_unique_rejects_a_second_serving_epoch(tmp_path):
    """The partial unique index enforces at most ONE serving epoch per generation."""
    conn = db.connect(tmp_path)
    gen_id = "g-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    e1 = "e-11111111111111111111111111111111"
    e2 = "e-22222222222222222222222222222222"
    try:
        conn.execute(
            "INSERT INTO generations (generation_id, build_id, lifecycle_state, "
            "current_epoch, capability_snapshot, created_at) VALUES (?,?,?,?,?,?)",
            (gen_id, "b1", "active", None, None, 1000.0))
        conn.execute(
            "INSERT INTO epochs (generation_epoch, generation_id, process_state, "
            "retirement_state, certificate, final_high_water, incarnation_meta, "
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (e1, gen_id, "serving", "open", None, None, None, 1000.0))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO epochs (generation_epoch, generation_id, process_state, "
                "retirement_state, certificate, final_high_water, incarnation_meta, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (e2, gen_id, "serving", "open", None, None, None, 1000.0))
            conn.commit()
    finally:
        conn.close()


def test_composite_fk_rejects_wrong_current_epoch(tmp_path):
    """The composite FK rejects a current_epoch from another generation."""
    conn = db.connect(tmp_path)
    try:
        g1 = "g-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        g2 = "g-cccccccccccccccccccccccccccccccc"
        e1 = "e-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        conn.execute(
            "INSERT INTO generations (generation_id, build_id, lifecycle_state, "
            "current_epoch, capability_snapshot, created_at) VALUES (?,?,?,?,?,?)",
            (g1, "b1", "active", None, None, 1000.0))
        conn.execute(
            "INSERT INTO generations (generation_id, build_id, lifecycle_state, "
            "current_epoch, capability_snapshot, created_at) VALUES (?,?,?,?,?,?)",
            (g2, "b1", "active", None, None, 1001.0))
        conn.execute(
            "INSERT INTO epochs (generation_epoch, generation_id, process_state, "
            "retirement_state, certificate, final_high_water, incarnation_meta, "
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (e1, g1, "serving", "open", None, None, None, 1002.0))
        # Try to set g2's current_epoch to g1's epoch — composite FK should reject.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE generations SET current_epoch=? WHERE generation_id=?",
                (e1, g2))
            conn.commit()
    finally:
        conn.close()


# ---- CRUD: create_generation ----

def test_create_generation_and_get(tmp_path):
    s = _store(tmp_path)
    try:
        s.create_generation("g-11111111111111111111111111111111",
                            build_id="b1", lifecycle_state="active",
                            capability_snapshot=None, created_at=1000.0)
        gr = s.get_generation("g-11111111111111111111111111111111")
        assert gr.generation_id == "g-11111111111111111111111111111111"
        assert gr.build_id == "b1"
        assert gr.lifecycle_state == "active"
        assert gr.current_epoch is None
        assert gr.created_at == 1000.0
    finally:
        s.close()


def test_create_generation_duplicate(tmp_path):
    s = _store(tmp_path)
    try:
        s.create_generation("g-11111111111111111111111111111111",
                            build_id="b1", lifecycle_state="active",
                            capability_snapshot=None, created_at=1000.0)
        with pytest.raises(NelixError) as ei:
            s.create_generation("g-11111111111111111111111111111111",
                                build_id="b2", lifecycle_state="active",
                                capability_snapshot=None, created_at=1000.0)
        assert ei.value.code == DUPLICATE_START
    finally:
        s.close()


def test_create_generation_invalid_id(tmp_path):
    s = _store(tmp_path)
    try:
        with pytest.raises(NelixError) as ei:
            s.create_generation("", build_id=None, lifecycle_state="active",
                                capability_snapshot=None, created_at=1000.0)
        assert ei.value.code == INVALID_REQUEST
    finally:
        s.close()


# ---- CRUD: insert_epoch ----

def test_insert_epoch(tmp_path):
    s = _store(tmp_path)
    try:
        s.create_generation("g-11111111111111111111111111111111",
                            build_id="b1", lifecycle_state="active",
                            capability_snapshot=None, created_at=1000.0)
        s.insert_epoch("e-11111111111111111111111111111111",
                       "g-11111111111111111111111111111111",
                       incarnation_meta=None, created_at=1001.0)
        eps = s.list_epochs("g-11111111111111111111111111111111")
        assert len(eps) == 1
        assert eps[0].generation_epoch == "e-11111111111111111111111111111111"
        assert eps[0].process_state == "starting"
        assert eps[0].retirement_state == "open"
    finally:
        s.close()


def test_insert_epoch_nonexistent_generation(tmp_path):
    s = _store(tmp_path)
    try:
        with pytest.raises(NelixError) as ei:
            s.insert_epoch("e-11111111111111111111111111111111",
                           "g-nonexistent",
                           incarnation_meta=None, created_at=1000.0)
        assert ei.value.code == UNKNOWN_SESSION
    finally:
        s.close()


# ---- CAS: cas_epoch_serving ----

def test_cas_epoch_serving_full_sequence(tmp_path):
    s = _store(tmp_path)
    try:
        gid = "g-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        eid = "e-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        s.create_generation(gid, build_id="b1", lifecycle_state="active",
                            capability_snapshot=None, created_at=1000.0)
        s.insert_epoch(eid, gid, incarnation_meta=None, created_at=1001.0)
        # CAS with expected_current_epoch=None (no current yet)
        s.cas_epoch_serving(gid, eid, expected_current_epoch=None)
        gr = s.get_generation(gid)
        assert gr.current_epoch == eid
        eps = s.list_epochs(gid)
        assert eps[0].process_state == "serving"
    finally:
        s.close()


def test_cas_epoch_serving_wrong_expectation(tmp_path):
    s = _store(tmp_path)
    try:
        gid = "g-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        eid = "e-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        s.create_generation(gid, build_id="b1", lifecycle_state="active",
                            capability_snapshot=None, created_at=1000.0)
        s.insert_epoch(eid, gid, incarnation_meta=None, created_at=1001.0)
        with pytest.raises(NelixError) as ei:
            s.cas_epoch_serving(gid, eid, expected_current_epoch="wrong")
        assert ei.value.code == IDEMPOTENCY_CONFLICT
    finally:
        s.close()


def test_cas_epoch_serving_nonexistent_generation(tmp_path):
    s = _store(tmp_path)
    try:
        with pytest.raises(NelixError) as ei:
            s.cas_epoch_serving("g-no-such-generation",
                                "e-11111111111111111111111111111111",
                                expected_current_epoch=None)
        assert ei.value.code == UNKNOWN_SESSION
    finally:
        s.close()


# ---- State transitions ----

def test_set_epoch_process_state(tmp_path):
    s = _store(tmp_path)
    try:
        gid = "g-cccccccccccccccccccccccccccccccc"
        eid = "e-cccccccccccccccccccccccccccccccc"
        s.create_generation(gid, build_id="b1", lifecycle_state="active",
                            capability_snapshot=None, created_at=1000.0)
        s.insert_epoch(eid, gid, incarnation_meta=None, created_at=1001.0)
        s.set_epoch_process_state(eid, "dead")
        eps = s.list_epochs(gid)
        assert eps[0].process_state == "dead"
    finally:
        s.close()


def test_set_epoch_retirement_certified(tmp_path):
    s = _store(tmp_path)
    try:
        gid = "g-dddddddddddddddddddddddddddddddd"
        eid = "e-dddddddddddddddddddddddddddddddd"
        s.create_generation(gid, build_id="b1", lifecycle_state="active",
                            capability_snapshot=None, created_at=1000.0)
        s.insert_epoch(eid, gid, incarnation_meta=None, created_at=1001.0)
        s.set_epoch_retirement(eid, retirement_state="certified",
                               certificate='{"ok":true}', final_high_water=42)
        eps = s.list_epochs(gid)
        assert eps[0].retirement_state == "certified"
        assert eps[0].certificate == '{"ok":true}'
        assert eps[0].final_high_water == 42
    finally:
        s.close()


def test_set_epoch_retirement_nonexistent(tmp_path):
    s = _store(tmp_path)
    try:
        with pytest.raises(NelixError) as ei:
            s.set_epoch_retirement("e-no-such", retirement_state="quiescing")
        assert ei.value.code == UNKNOWN_SESSION
    finally:
        s.close()


# ---- set_generation_lifecycle_state & clear_current_epoch ----

def test_set_generation_lifecycle_state(tmp_path):
    s = _store(tmp_path)
    try:
        gid = "g-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        s.create_generation(gid, build_id="b1", lifecycle_state="active",
                            capability_snapshot=None, created_at=1000.0)
        s.set_generation_lifecycle_state(gid, "retired")
        gr = s.get_generation(gid)
        assert gr.lifecycle_state == "retired"
    finally:
        s.close()


def test_clear_current_epoch(tmp_path):
    s = _store(tmp_path)
    try:
        gid = "g-ffffffffffffffffffffffffffffffff"
        eid = "e-ffffffffffffffffffffffffffffffff"
        s.create_generation(gid, build_id="b1", lifecycle_state="active",
                            capability_snapshot=None, created_at=1000.0)
        s.insert_epoch(eid, gid, incarnation_meta=None, created_at=1001.0)
        s.cas_epoch_serving(gid, eid, expected_current_epoch=None)
        assert s.get_generation(gid).current_epoch == eid
        s.clear_current_epoch(gid)
        assert s.get_generation(gid).current_epoch is None
    finally:
        s.close()


# ---- list_epochs ----

def test_list_epochs_returns_multiple(tmp_path):
    s = _store(tmp_path)
    try:
        gid = "g-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        s.create_generation(gid, build_id="b1", lifecycle_state="active",
                            capability_snapshot=None, created_at=1000.0)
        for i in range(3):
            s.insert_epoch(f"e-{i:032x}", gid,
                           incarnation_meta=None, created_at=1001.0 + i)
        eps = s.list_epochs(gid)
        assert len(eps) == 3
    finally:
        s.close()


def test_list_epochs_empty(tmp_path):
    s = _store(tmp_path)
    try:
        eps = s.list_epochs("g-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        assert eps == []
    finally:
        s.close()


# ---- set_generation_confirmed_high_water ----

def test_set_generation_confirmed_high_water_monotonic(tmp_path):
    s = _store(tmp_path)
    try:
        epoch = "e-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        s.set_generation_confirmed_high_water(epoch, 5)
        s.set_generation_confirmed_high_water(epoch, 3)   # no regress
        gp = s._conn.execute(
            "SELECT confirmed_high_water FROM generation_progress WHERE generation_id=?",
            (epoch,)).fetchone()
        assert gp["confirmed_high_water"] == 5
    finally:
        s.close()


# ---- v3→v4 migration tests (nelix-80e-s1a) ----

def _build_v3_database(tmp_path, label="test"):
    """Create a v3 database with populated data. Returns (db_path, ctx)."""
    db_path = tmp_path / "nelix.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    # meta
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '3')")
    # v3 DDL (starts WITHOUT generation_epoch, terminal WITH terminal_seq)
    conn.execute("""
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
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS starts_by_owner ON starts (owner_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id     TEXT PRIMARY KEY REFERENCES starts (session_id) ON DELETE RESTRICT,
            state          TEXT NOT NULL,
            executor       TEXT NOT NULL,
            task           TEXT NOT NULL,
            cwd            TEXT NOT NULL,
            model          TEXT,
            created_at     REAL NOT NULL,
            schema_version INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS terminal (
            session_id      TEXT PRIMARY KEY REFERENCES sessions (session_id) ON DELETE RESTRICT,
            terminal_kind   TEXT NOT NULL,
            summary         TEXT NOT NULL,
            ended_at        REAL NOT NULL,
            published_at    REAL NOT NULL,
            terminal_seq    INTEGER NOT NULL DEFAULT 0,
            acknowledged_at REAL,
            expired_at      REAL,
            expire_reason   TEXT,
            schema_version  INTEGER NOT NULL,
            CHECK ((expired_at IS NULL) = (expire_reason IS NULL)),
            CHECK (expire_reason IS NULL OR expire_reason IN ('age', 'count')),
            CHECK (expired_at IS NULL OR acknowledged_at IS NULL)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS terminal_by_published ON terminal (published_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS generation_progress (
            generation_id        TEXT PRIMARY KEY,
            next_terminal_seq    INTEGER NOT NULL DEFAULT 1,
            confirmed_high_water INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Populate: two legacy epochs, several sessions/terminals each.
    epoch1 = "ep-legacy-1"
    epoch2 = "ep-legacy-2"
    sids1 = []
    sids2 = []

    def make_start(sid, owner, gid, key, ts=1.0):
        conn.execute(
            "INSERT INTO starts (session_id, owner_id, orchestration_id, "
            "idempotency_key, request_fingerprint, state, generation_id, reason, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, owner, "o-" + "0" * 32, key, "fp", "starting", gid, None, ts))

    def make_session(sid, state="running", ts=1.0, sv=3):
        conn.execute(
            "INSERT INTO sessions (session_id, state, executor, task, cwd, model, "
            "created_at, schema_version) VALUES (?,?,?,?,?,?,?,?)",
            (sid, state, "coder", "task", "/repo", None, ts, sv))

    def make_terminal(sid, kind="done", summary="s", ended=5.0, published=1000.0,
                      seq=0, sv=3):
        conn.execute(
            "INSERT INTO terminal (session_id, terminal_kind, summary, ended_at, "
            "published_at, terminal_seq, acknowledged_at, schema_version) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sid, kind, summary, ended, published, seq, None, sv))

    # Generation 1: 2 terminals (seq 1, 2)
    for i in range(2):
        sid = f"s-{i+1:032x}"
        make_start(sid, "hermes:local", epoch1, f"kg1-{i}")
        make_session(sid, ts=10.0 + i)
        make_terminal(sid, ended=100.0 + i, published=2000.0 + i, seq=i + 1)
        sids1.append(sid)
    conn.execute(
        "INSERT OR IGNORE INTO generation_progress "
        "(generation_id, next_terminal_seq, confirmed_high_water) "
        "VALUES (?, ?, 0)", (epoch1, 3))

    # Generation 2: no terminals (empty epoch)
    sid = f"s-{3:032x}"
    make_start(sid, "hermes:local", epoch2, "kg2-0")
    make_session(sid, ts=20.0)
    sids2.append(sid)

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return {"epoch1": epoch1, "epoch2": epoch2, "sids1": sids1, "sids2": sids2}


def test_v3_to_v4_migration_backfills_identity(tmp_path):
    """Seed a v3 DB, open via Store (triggers migration), verify backfill."""
    ctx = _build_v3_database(tmp_path)
    s = _store(tmp_path)
    try:
        # Verify stamp
        stamp = s._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        assert stamp == str(SCHEMA_VERSION)

        # Generations table has synthetic rows
        gen1 = s.get_generation("g-legacy-" + ctx["epoch1"])
        assert gen1.lifecycle_state == "retired"
        gen2 = s.get_generation("g-legacy-" + ctx["epoch2"])
        assert gen2 is not None

        # Epochs table has synthetic rows
        eps1 = s.list_epochs("g-legacy-" + ctx["epoch1"])
        assert len(eps1) == 1
        assert eps1[0].generation_epoch == ctx["epoch1"]
        assert eps1[0].process_state == "dead"
        assert eps1[0].retirement_state == "certified"
        assert eps1[0].final_high_water == 2  # max seq for epoch1

        eps2 = s.list_epochs("g-legacy-" + ctx["epoch2"])
        assert len(eps2) == 1
        # Epoch2 has no terminals → final_high_water is None
        assert eps2[0].final_high_water is None

        # starts.generation_epoch = old epoch, starts.generation_id = synthetic
        for sid in ctx["sids1"]:
            row = s._conn.execute(
                "SELECT generation_id, generation_epoch FROM starts WHERE session_id=?",
                (sid,)).fetchone()
            assert row["generation_id"] == "g-legacy-" + ctx["epoch1"]
            assert row["generation_epoch"] == ctx["epoch1"]

        # Row-level schema_version bumped
        rows = s._conn.execute(
            "SELECT schema_version FROM sessions WHERE schema_version<4").fetchall()
        assert rows == []
        rows = s._conn.execute(
            "SELECT schema_version FROM terminal WHERE schema_version<4").fetchall()
        assert rows == []
    finally:
        s.close()


def test_v3_migration_preserves_list_sessions_and_list_terminal(tmp_path):
    """After v3→v4 migration, list_sessions/list_terminal still return the same rows."""
    ctx = _build_v3_database(tmp_path)
    s = _store(tmp_path)
    try:
        sessions = s.list_sessions("hermes:local")
        sess_ids = {r.session_id for r in sessions}
        expected = set(ctx["sids1"] + ctx["sids2"])
        missing = expected - sess_ids
        assert not missing, f"list_sessions dropped {len(missing)} rows: {missing}"
        assert len(sessions) == 3

        terminals = s.list_terminal("hermes:local")
        term_ids = {r.session_id for r in terminals}
        assert set(ctx["sids1"]) <= term_ids
        assert len(terminals) == 2  # epoch2 has no terminals
    finally:
        s.close()


def test_v2_to_v4_chained_migration(tmp_path):
    """A v2 database reaches v4 through both migration steps."""
    from tests.test_store_db import _build_v2_database
    ctx = _build_v2_database(tmp_path / "nelix.db")
    s = _store(tmp_path)
    try:
        stamp = s._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        assert stamp == str(SCHEMA_VERSION)

        # Confirm v2 data is preserved through both migrations
        sessions = s.list_sessions("hermes:local")
        assert len(sessions) == 5

        terminals = s.list_terminal("hermes:local")
        assert len(terminals) == 5

        # Synthetic generations/epochs exist for both legacy epochs from v2
        for gid in [ctx["gid1"], ctx["gid2"]]:
            sid = "g-legacy-" + gid
            gen = s.get_generation(sid)
            assert gen is not None
            eps = s.list_epochs(sid)
            assert len(eps) == 1
            assert eps[0].generation_epoch == gid
            assert eps[0].retirement_state == "certified"
    finally:
        s.close()
