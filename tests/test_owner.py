"""The owner RECORD: shape rules, atomic durable write, and the fail-closed read.

The isolation these rules exist to buy is proved end-to-end, over real routes, in
tests/test_owner_isolation.py. This file pins the primitive underneath it.
"""
import json
import os

import pytest

import paths
from daemon import owner


def _dir(tmp_path):
    d = tmp_path / "s-abcd1234"
    d.mkdir()
    return d


# ---------------------------------------------------------------- shape

@pytest.mark.parametrize("good", [
    "o", "hermes", "claude-code", "o-" + "a" * 32, "Team.A_x", "a:b", "x" * 128,
])
def test_validate_accepts(good):
    assert owner.validate(good) == good


@pytest.mark.parametrize("bad", [
    None, "", 7, True, b"hermes", ["hermes"],
    "-leading-dash",          # leading char is alnum so a value can never read as a flag
    ".dotfirst", ":colonfirst",
    "has space", "has\ttab", "has\nnewline", "has/slash", "../escape", "quo'te",
    "x" * 129,                # 128 is the cap
    "héllo",                  # ascii-only charset
])
def test_validate_rejects(bad):
    with pytest.raises(owner.OwnerRejected):
        owner.validate(bad)


def test_validate_never_coerces():
    # A defaulted/coerced owner is a SHARED owner, which is the exact bug this prevents.
    with pytest.raises(owner.OwnerRejected):
        owner.validate("  hermes  ")


# ---------------------------------------------------------------- write

def test_write_then_read_roundtrip(tmp_path):
    d = _dir(tmp_path)
    owner.write(d, "hermes")
    assert owner.owner_of(d) == "hermes"


def test_write_is_private_and_atomic(tmp_path):
    d = _dir(tmp_path)
    owner.write(d, "hermes")
    p = paths.session_owner(d)
    assert oct(p.stat().st_mode)[-3:] == "600"       # same discipline as the raw transcript
    assert list(d.glob("*.tmp")) == []               # no torn temp left behind
    assert json.loads(p.read_text()) == {"owner_id": "hermes"}


def test_write_rejects_bad_shape_before_touching_disk(tmp_path):
    d = _dir(tmp_path)
    with pytest.raises(owner.OwnerRejected):
        owner.write(d, "has space")
    assert not paths.session_owner(d).exists()


def test_write_creates_the_session_dir_if_absent(tmp_path):
    # The owner is written BEFORE the Dialog builds the dir, so write() must not depend on it.
    d = tmp_path / "s-notyet"
    owner.write(d, "hermes")
    assert owner.owner_of(d) == "hermes"


def test_write_raises_owner_write_failed_when_unwritable(tmp_path, monkeypatch):
    d = _dir(tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(owner, "open", boom, raising=False)
    with pytest.raises(owner.OwnerWriteFailed):
        owner.write(d, "hermes")
    # It must NOT be a ValueError: /start maps ValueError to a 4xx "your input was bad", and a
    # full disk is not the caller's mistake.
    assert not isinstance(owner.OwnerWriteFailed("x"), ValueError)


def test_write_overwrites_a_prior_record_atomically(tmp_path):
    d = _dir(tmp_path)
    owner.write(d, "hermes")
    owner.write(d, "claude")
    assert owner.owner_of(d) == "claude"
    assert list(d.glob("*.tmp")) == []


# ---------------------------------------------------------------- read: FAIL CLOSED

def test_owner_of_missing_dir_is_none(tmp_path):
    assert owner.owner_of(tmp_path / "nope") is None


def test_owner_of_missing_record_is_none(tmp_path):
    assert owner.owner_of(_dir(tmp_path)) is None


@pytest.mark.parametrize("junk", [
    "",                       # truncated to nothing
    "{",                      # torn write
    "null", "[]", '"hermes"', "42",           # valid JSON, wrong shape
    "{}",                                     # object, no owner_id
    '{"owner_id": null}', '{"owner_id": 7}',  # present, wrong type
    '{"owner_id": "has space"}',              # present, bad shape
    '{"owner_id": ""}',
])
def test_owner_of_malformed_is_none(tmp_path, junk):
    d = _dir(tmp_path)
    paths.session_owner(d).write_text(junk)
    assert owner.owner_of(d) is None, f"malformed record {junk!r} did not fail closed"


def test_owner_of_unreadable_record_is_none(tmp_path):
    d = _dir(tmp_path)
    p = paths.session_owner(d)
    p.write_text('{"owner_id": "hermes"}')
    os.chmod(p, 0o000)
    try:
        assert owner.owner_of(d) is None
    finally:
        os.chmod(p, 0o600)                    # so tmp_path cleanup can remove it


# ---------------------------------------------------------------- owns

def test_owns_is_true_only_for_the_stored_owner(tmp_path):
    d = _dir(tmp_path)
    owner.write(d, "hermes")
    assert owner.owns(d, "hermes")
    assert not owner.owns(d, "claude")


def test_owns_is_case_sensitive(tmp_path):
    d = _dir(tmp_path)
    owner.write(d, "hermes")
    assert not owner.owns(d, "Hermes")


@pytest.mark.parametrize("caller", [None, "", "has space", 7])
def test_owns_rejects_a_bad_caller_owner_against_a_valid_record(tmp_path, caller):
    d = _dir(tmp_path)
    owner.write(d, "hermes")
    assert not owner.owns(d, caller)


@pytest.mark.parametrize("caller", [None, "", "hermes"])
def test_owns_never_matches_an_unestablishable_owner(tmp_path, caller):
    # THE skeleton-key case: no record => owner_of is None. If owns() compared the raw values,
    # a caller omitting owner_id (None) would match None and own every ownerless session.
    d = _dir(tmp_path)
    assert not owner.owns(d, caller)


def test_owns_never_matches_a_malformed_record_even_with_the_same_string(tmp_path):
    # The stored value is bad-shape. A caller passing the IDENTICAL string must still be refused:
    # the record is not trustworthy, so it grants nothing.
    d = _dir(tmp_path)
    paths.session_owner(d).write_text(json.dumps({"owner_id": "has space"}))
    assert not owner.owns(d, "has space")


def test_owns_session_keys_off_the_sessions_root(tmp_path, monkeypatch):
    monkeypatch.setenv("NELIX_HOME", str(tmp_path))
    owner.write(paths.sessions_root() / "s-1", "hermes")
    assert owner.owns_session("s-1", "hermes")
    assert not owner.owns_session("s-1", "claude")
    assert not owner.owns_session("s-unknown", "hermes")
