"""S2a.1: cursor archive component — typed archive_position, advance_archive,
v1 token expiry, and validation.

Tests EVERY Acceptance clause from the spec: round-trip, v1 expired,
advance_archive isolation, and validation of negative/bool/non-int values.
"""
import pytest

from nelix_contracts.cursor import (
    CURSOR_EXPIRED, INVALID_REQUEST, decode, encode, new_cursor,
)


def _v1_token(router_epoch="re-1", topology_revision=0):
    """Produce a v1 token (no archive component) for testing expiry."""
    import base64, json
    raw = json.dumps({
        "v": 1, "re": router_epoch, "tr": topology_revision, "p": {},
    }, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# ---- round-trip ------------------------------------------------------------

def test_round_trip_cursor_without_archive():
    c = new_cursor("re-1", 0)
    token = encode(c)
    decoded = decode(token, router_epoch="re-1", topology_revision=0)
    assert decoded.archive_position is None
    assert decoded.router_epoch == "re-1"
    assert decoded.topology_revision == 0


def test_round_trip_cursor_with_archive():
    c = new_cursor("re-2", 1).advance_archive(42, 7)
    token = encode(c)
    decoded = decode(token, router_epoch="re-2", topology_revision=1)
    assert decoded.archive_position == (42, 7)
    assert decoded.positions == {}


def test_round_trip_cursor_with_archive_and_positions():
    c = new_cursor("re-3", 2)
    c = c.advance("g-" + "1" * 32, "g-" + "2" * 32, 3)
    c = c.advance_archive(99, 15)
    token = encode(c)
    decoded = decode(token, router_epoch="re-3", topology_revision=2)
    assert decoded.archive_position == (99, 15)
    assert decoded.positions is not None
    assert len(decoded.positions) == 1


# ---- advance_archive leaves generation positions untouched ------------------

def test_advance_archive_does_not_touch_positions():
    c = new_cursor("re-1", 0)
    c = c.advance("g-" + "3" * 32, "g-" + "4" * 32, 5)
    c2 = c.advance_archive(10, 1)
    assert c2.archive_position == (10, 1)
    # Positions must be unchanged.
    assert dict(c2.positions) == dict(c.positions)
    # Original cursor must be unchanged (frozen).
    assert c.archive_position is None


def test_advance_archive_called_multiple_times_updates_only_archive():
    c = new_cursor("re-1", 0)
    c = c.advance_archive(1, 1)
    c = c.advance_archive(1, 2)
    assert c.archive_position == (1, 2)


# ---- v1 token expiry -------------------------------------------------------

def test_v1_token_yields_cursor_expired():
    old_token = _v1_token()
    with pytest.raises(Exception) as ei:
        decode(old_token, router_epoch="re-1", topology_revision=0)
    assert ei.value.code == CURSOR_EXPIRED


def test_v1_token_with_current_router_epoch_still_expired():
    old_token = _v1_token(router_epoch="re-x")
    with pytest.raises(Exception) as ei:
        decode(old_token, router_epoch="re-x", topology_revision=0)
    assert ei.value.code == CURSOR_EXPIRED


# ---- validation: negative seq ----------------------------------------------

def test_advance_archive_rejects_negative_seq():
    c = new_cursor("re-1", 0)
    with pytest.raises(Exception) as ei:
        c.advance_archive(1, -1)
    assert ei.value.code == INVALID_REQUEST


def test_decode_rejects_archive_with_negative_seq():
    import base64, json
    raw = json.dumps({
        "v": 2, "re": "re-1", "tr": 0, "p": {}, "a": [1, -5],
    }, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with pytest.raises(Exception) as ei:
        decode(token, router_epoch="re-1", topology_revision=0)
    assert ei.value.code == INVALID_REQUEST


# ---- validation: bool seq --------------------------------------------------

def test_advance_archive_rejects_bool_seq():
    c = new_cursor("re-1", 0)
    with pytest.raises(Exception) as ei:
        c.advance_archive(1, True)
    assert ei.value.code == INVALID_REQUEST


def test_decode_rejects_archive_with_bool_seq():
    import base64, json
    raw = json.dumps({
        "v": 2, "re": "re-1", "tr": 0, "p": {}, "a": [1, True],
    }, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with pytest.raises(Exception) as ei:
        decode(token, router_epoch="re-1", topology_revision=0)
    assert ei.value.code == INVALID_REQUEST


# ---- validation: non-int epoch ---------------------------------------------

def test_advance_archive_rejects_non_int_epoch():
    c = new_cursor("re-1", 0)
    with pytest.raises(Exception) as ei:
        c.advance_archive("not-an-int", 1)
    assert ei.value.code == INVALID_REQUEST


def test_decode_rejects_archive_with_string_epoch():
    import base64, json
    raw = json.dumps({
        "v": 2, "re": "re-1", "tr": 0, "p": {}, "a": ["string-epoch", 1],
    }, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with pytest.raises(Exception) as ei:
        decode(token, router_epoch="re-1", topology_revision=0)
    assert ei.value.code == INVALID_REQUEST


# ---- archive_position returns None when not set ----------------------------

def test_archive_position_is_none_for_new_cursor():
    c = new_cursor("re-1", 0)
    assert c.archive_position is None


def test_archive_position_is_none_after_only_advance():
    c = new_cursor("re-1", 0)
    c = c.advance("g-" + "5" * 32, "g-" + "6" * 32, 1)
    assert c.archive_position is None


# ---- encode/decode with no archive yields None -----------------------------

def test_decode_token_without_archive_returns_none():
    c = new_cursor("re-1", 5)
    token = encode(c)
    decoded = decode(token, router_epoch="re-1", topology_revision=5)
    assert decoded.archive_position is None
