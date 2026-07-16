from nelix_contracts.retirement import may_retire, retirement_blockers


def test_a_generation_with_no_work_left_may_retire():
    assert may_retire(live_pty_count=0, unpersisted_terminal_count=0,
                      router_confirmed_visible=True) is True


def test_live_ptys_block_retirement():
    assert may_retire(live_pty_count=1, unpersisted_terminal_count=0,
                      router_confirmed_visible=True) is False


def test_zero_ptys_is_not_enough_when_results_are_not_yet_durable():
    # "N-1 exits at zero" is wrong: zero live PTYs != zero routable state. If the generation
    # exits now, its in-memory ring and terminal inventory vanish WITH it and the final wake
    # — the one that matters most — is lost.
    assert may_retire(live_pty_count=0, unpersisted_terminal_count=1,
                      router_confirmed_visible=True) is False


def test_the_router_must_confirm_the_results_are_visible_first():
    assert may_retire(live_pty_count=0, unpersisted_terminal_count=0,
                      router_confirmed_visible=False) is False


def test_blockers_name_every_reason_not_just_the_first():
    assert retirement_blockers(live_pty_count=2, unpersisted_terminal_count=1,
                               router_confirmed_visible=False) == (
        "live_ptys", "unpersisted_terminal_records", "router_has_not_confirmed_visibility")


def test_no_blockers_when_ready():
    assert retirement_blockers(live_pty_count=0, unpersisted_terminal_count=0,
                               router_confirmed_visible=True) == ()
