import pytest

from nelix_contracts import errors
from nelix_contracts.cursor import Cursor, decode, encode, new_cursor
from nelix_contracts.errors import NelixError

GEN_A = "g-" + "a" * 32
GEN_B = "g-" + "b" * 32
EPOCH = "re-1111"


def test_round_trip_preserves_every_component():
    c = new_cursor(EPOCH, 3).advance(GEN_A, "ea", 7).advance(GEN_B, "eb", 2)
    back = decode(encode(c), router_epoch=EPOCH, topology_revision=3)
    assert back.position_for(GEN_A) == ("ea", 7)
    assert back.position_for(GEN_B) == ("eb", 2)
    assert back.topology_revision == 3


def test_advance_touches_only_its_own_component():
    # THE core invariant: two generations are two processes with no common clock. An event
    # from A must never imply progress in B, or B's events get silently skipped.
    c = new_cursor(EPOCH, 1).advance(GEN_A, "ea", 5).advance(GEN_B, "eb", 9)
    c2 = c.advance(GEN_A, "ea", 6)
    assert c2.position_for(GEN_A) == ("ea", 6)
    assert c2.position_for(GEN_B) == ("eb", 9)


def test_cursor_is_immutable():
    c = new_cursor(EPOCH, 1).advance(GEN_A, "ea", 1)
    c.advance(GEN_A, "ea", 2)
    assert c.position_for(GEN_A) == ("ea", 1)


def test_unknown_generation_has_no_position():
    # None means "never seen this generation — start from its beginning", which is what a
    # caller must do when a generation appears.
    assert new_cursor(EPOCH, 1).position_for(GEN_A) is None


def test_token_is_opaque_not_a_readable_id():
    token = encode(new_cursor(EPOCH, 1).advance(GEN_A, "ea", 5))
    assert GEN_A not in token
    assert "seq" not in token


def test_router_restart_expires_the_cursor():
    # A new router epoch means the old positions describe a routing world that is gone.
    token = encode(new_cursor(EPOCH, 1).advance(GEN_A, "ea", 5))
    with pytest.raises(NelixError) as ei:
        decode(token, router_epoch="re-2222", topology_revision=1)
    assert ei.value.code == errors.CURSOR_EXPIRED


def test_topology_change_reports_board_changed_not_expiry():
    # The positions are still valid; the SET of components is not. The caller must refetch
    # the board and re-arm, or it would never see the new generation's events.
    token = encode(new_cursor(EPOCH, 1).advance(GEN_A, "ea", 5))
    with pytest.raises(NelixError) as ei:
        decode(token, router_epoch=EPOCH, topology_revision=2)
    assert ei.value.code == errors.BOARD_CHANGED


@pytest.mark.parametrize("bad", ["", "!!!not-base64!!!", "YWJj", "x" * 9])
def test_malformed_token_is_an_invalid_request(bad):
    with pytest.raises(NelixError) as ei:
        decode(bad, router_epoch=EPOCH, topology_revision=1)
    assert ei.value.code == errors.INVALID_REQUEST


def test_positions_survive_json_key_ordering():
    c = new_cursor(EPOCH, 1).advance(GEN_B, "eb", 1).advance(GEN_A, "ea", 2)
    assert decode(encode(c), router_epoch=EPOCH, topology_revision=1) == c


import json, base64


def _token(payload):
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_a_future_version_token_expires_even_when_its_body_changed_shape():
    # THE bug rev 1 shipped: the version check ran after the body parse, so a v2 token whose
    # positions grew a field reported invalid_request (caller's fault) instead of
    # cursor_expired (refetch the board). A version bump that changes nothing is not a
    # version bump worth having.
    token = _token({"v": 2, "re": EPOCH, "tr": 1, "p": {GEN_A: ["ea", 5, "extra"]}})
    with pytest.raises(NelixError) as ei:
        decode(token, router_epoch=EPOCH, topology_revision=1)
    assert ei.value.code == errors.CURSOR_EXPIRED


def test_a_future_version_token_missing_a_field_still_expires():
    token = _token({"v": 2, "re": EPOCH, "tr": 1})
    with pytest.raises(NelixError) as ei:
        decode(token, router_epoch=EPOCH, topology_revision=1)
    assert ei.value.code == errors.CURSOR_EXPIRED


def test_positions_cannot_be_mutated_through_a_frozen_cursor():
    # frozen=True is shallow: it protects the attribute binding, not the dict behind it.
    c = new_cursor(EPOCH, 1).advance(GEN_A, "ea", 5)
    with pytest.raises(TypeError):
        c.positions[GEN_A] = ("wrong", -1)


def test_advance_refuses_to_rewind_a_component():
    # A cursor that can go backwards re-delivers events the caller already handled.
    c = new_cursor(EPOCH, 1).advance(GEN_A, "ea", 10)
    with pytest.raises(NelixError) as ei:
        c.advance(GEN_A, "ea", 3)
    assert ei.value.code == errors.INVALID_REQUEST


def test_advance_allows_the_same_seq_again():
    # Idempotent re-delivery of the same position is not a rewind.
    c = new_cursor(EPOCH, 1).advance(GEN_A, "ea", 10)
    assert c.advance(GEN_A, "ea", 10).position_for(GEN_A) == ("ea", 10)


def test_advance_accepts_a_reset_seq_when_the_generation_epoch_changes():
    # A generation restarted: new epoch, its seq legitimately restarts from 1.
    c = new_cursor(EPOCH, 1).advance(GEN_A, "ea", 10)
    assert c.advance(GEN_A, "eb", 1).position_for(GEN_A) == ("eb", 1)


@pytest.mark.parametrize("seq", [-1, 1.9, "3", None, True])
def test_advance_rejects_a_non_integral_seq(seq):
    with pytest.raises(NelixError):
        new_cursor(EPOCH, 1).advance(GEN_A, "ea", seq)


def test_advance_rejects_a_malformed_generation_id():
    with pytest.raises(NelixError):
        new_cursor(EPOCH, 1).advance("not-a-generation", "ea", 1)


def test_a_cursor_cannot_be_constructed_invalid():
    # records.py validates in __post_init__ so a record is always valid by construction.
    # Cursor did not, so the MappingProxy and the validation only protected the helper path.
    with pytest.raises(NelixError):
        Cursor(router_epoch=EPOCH, topology_revision=-3, positions={})
    with pytest.raises(NelixError):
        Cursor(router_epoch=None, topology_revision=1, positions={})
    with pytest.raises(NelixError):
        Cursor(router_epoch=EPOCH, topology_revision=1, positions={"not-a-gen": ("e", 1)})
    with pytest.raises(NelixError):
        Cursor(router_epoch=EPOCH, topology_revision=1, positions={GEN_A: ("e", -1)})


def test_a_directly_constructed_cursor_still_has_immutable_positions():
    c = Cursor(router_epoch=EPOCH, topology_revision=1, positions={GEN_A: ("ea", 1)})
    with pytest.raises(TypeError):
        c.positions[GEN_A] = ("wrong", 9)


def test_decode_does_not_coerce_a_fractional_topology_revision():
    token = _token({"v": 1, "re": EPOCH, "tr": 1.9, "p": {}})
    with pytest.raises(NelixError) as ei:
        decode(token, router_epoch=EPOCH, topology_revision=1)
    assert ei.value.code == errors.INVALID_REQUEST


def test_json_true_is_not_cursor_version_one():
    # True == 1 in Python, so a bare equality check accepts `true` as the version.
    token = _token({"v": True, "re": EPOCH, "tr": 1, "p": {}})
    with pytest.raises(NelixError):
        decode(token, router_epoch=EPOCH, topology_revision=1)
