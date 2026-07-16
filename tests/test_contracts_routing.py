import pytest

from nelix_contracts import routing
from nelix_contracts.routing import ALL_CLASSES, OPERATION_CLASS, UnknownOperation, classify


def test_start_goes_to_the_active_generation():
    assert classify("start") == routing.ACTIVE_GENERATION


@pytest.mark.parametrize("op", ["respond", "stop", "restart", "screen", "dialog", "ack_terminal"])
def test_session_scoped_operations_route_by_session(op):
    # Including `dialog` and `screen`: they read a session's transcript/screen, so they must be
    # resolved through the owning session like any mutation — a session id alone must never
    # be enough to reach them.
    assert classify(op) == routing.SESSION_KEYED


@pytest.mark.parametrize("op", ["status", "wait"])
def test_board_operations_fan_out(op):
    assert classify(op) == routing.FAN_OUT


@pytest.mark.parametrize("op", ["generation_install", "generation_activate",
                               "generation_retire", "generation_list"])
def test_generation_lifecycle_is_operator_local(op):
    assert classify(op) == routing.OPERATOR


def test_unknown_operation_raises_rather_than_defaulting():
    # Defaulting an unknown op to any class would silently mis-route it.
    with pytest.raises(UnknownOperation):
        classify("delete_everything")


def test_the_operation_set_is_exactly_the_contract():
    # An authoritative set, asserted by equality. The old version iterated the dict's own
    # values, so a MISSING operation could never fail it — which is how /hook and /message
    # went absent.
    assert set(OPERATION_CLASS) == {
        "start",
        "respond", "stop", "restart", "screen", "dialog", "ack_terminal", "hook", "message",
        "status", "wait",
        "generation_install", "generation_activate", "generation_retire", "generation_list",
        "capabilities",
    }
    assert set(OPERATION_CLASS.values()) <= ALL_CLASSES


@pytest.mark.parametrize("op", ["hook", "message"])
def test_the_executor_facing_plane_routes_by_session(op):
    # A worker's hook must reach ITS generation. Note these authenticate by per-session
    # secret, NOT by owner_id — routing them is this table's job; authorising them is not.
    assert classify(op) == routing.SESSION_KEYED


def test_capabilities_is_router_local_not_fanned_out():
    # Fanning capabilities out would merge N generations' answers into one, which is exactly
    # the thing a per-session capability check exists to avoid.
    assert classify("capabilities") == routing.OPERATOR
