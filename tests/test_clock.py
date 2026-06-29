from daemon.clock import WallClock, FakeClock


def test_fake_clock_starts_at_value():
    assert FakeClock(100.0).now() == 100.0


def test_fake_clock_advances():
    c = FakeClock(100.0)
    assert c.advance(2.5) == 102.5
    assert c.now() == 102.5
    c.advance(0.5)
    assert c.now() == 103.0


def test_wall_clock_returns_float():
    assert isinstance(WallClock().now(), float)


def test_wall_clock_is_monotonic_nondecreasing():
    c = WallClock()
    a = c.now()
    b = c.now()
    assert b >= a
