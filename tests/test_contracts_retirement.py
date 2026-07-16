import pytest

from nelix_contracts.retirement import may_retire, retirement_blockers

READY = dict(live_pty_count=0, inflight_or_starting_count=0,
             terminal_persisted_high_water=7, router_visible_high_water=7)


def test_a_generation_with_no_work_left_may_retire():
    assert may_retire(**READY) is True


def test_live_ptys_block_retirement():
    assert may_retire(**{**READY, "live_pty_count": 1}) is False


def test_a_start_in_flight_blocks_retirement_even_with_zero_ptys():
    # A reservation is already assigned to this generation but its PTY does not exist yet.
    # "Zero PTYs" would wave it through and the start would land on a dead generation.
    assert may_retire(**{**READY, "inflight_or_starting_count": 1}) is False


def test_a_result_the_router_has_not_seen_yet_blocks_retirement():
    # THE ordering bug a bool could not express: the router confirmed through record 7, then
    # record 8 was persisted. A stale "confirmed" flag would let the generation exit and take
    # record 8 — the final wake — with it.
    assert may_retire(**{**READY, "terminal_persisted_high_water": 8,
                         "router_visible_high_water": 7}) is False


def test_a_router_ahead_of_the_generation_does_not_block():
    assert may_retire(**{**READY, "router_visible_high_water": 9}) is True


def test_blockers_name_every_reason_not_just_the_first():
    assert retirement_blockers(live_pty_count=2, inflight_or_starting_count=1,
                               terminal_persisted_high_water=8,
                               router_visible_high_water=7) == (
        "live_ptys", "starts_in_flight", "terminal_records_not_yet_visible_to_router")


def test_no_blockers_when_ready():
    assert retirement_blockers(**READY) == ()


@pytest.mark.parametrize("field", ["live_pty_count", "inflight_or_starting_count",
                                   "terminal_persisted_high_water", "router_visible_high_water"])
def test_a_negative_count_is_a_bug_not_a_green_light(field):
    # rev 1 treated a negative count as zero and PERMITTED retirement.
    with pytest.raises(ValueError):
        may_retire(**{**READY, field: -1})
