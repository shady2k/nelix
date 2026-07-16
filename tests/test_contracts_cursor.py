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
