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


