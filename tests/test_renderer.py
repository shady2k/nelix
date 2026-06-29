from daemon.renderer.base import Frame, make_renderer


def test_frame_fields_and_defaults():
    f = Frame(rows=["a", ""], cursor=(1, 2), cursor_visible=True, alt_screen=False)
    assert f.rows == ["a", ""]
    assert f.cursor == (1, 2)
    assert f.cursor_visible is True
    assert f.alt_screen is False
    assert f.dirty is None and f.wrap is None


def test_make_renderer_returns_a_working_renderer():
    r = make_renderer(cols=40, rows=5)
    try:
        r.feed(b"hi")
        frame = r.snapshot()
        assert len(frame.rows) == 5                 # rectangular: exactly rows
        assert frame.rows[0].startswith("hi")
        assert r.render() == "\n".join(frame.rows)
    finally:
        r.close()
