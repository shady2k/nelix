import gc
from daemon.renderer.ghostty import GhosttyRenderer


def _render(data, cols=80, rows=24):
    r = GhosttyRenderer(cols=cols, rows=rows)
    try:
        r.feed(data)
        return r
    finally:
        pass  # caller closes


def test_feed_and_snapshot_basic():
    r = GhosttyRenderer(cols=80, rows=24)
    try:
        r.feed(b"hello\r\nworld")
        f = r.snapshot()
        assert len(f.rows) == 24                    # rectangular
        assert f.rows[0].startswith("hello")
        assert f.rows[1].startswith("world")
        assert f.alt_screen is False
        assert r.render() == "\n".join(f.rows)
    finally:
        r.close()


def test_incremental_feed_matches_one_shot():
    a = GhosttyRenderer(cols=80, rows=24)
    b = GhosttyRenderer(cols=80, rows=24)
    try:
        a.feed(b"\x1b[2J\x1b[H")
        a.feed(b"line-a\r\n")
        a.feed(b"line-b")
        b.feed(b"\x1b[2J\x1b[Hline-a\r\nline-b")
        assert a.render() == b.render()             # parser state carries across feed() calls
    finally:
        a.close(); b.close()


def test_alt_screen_flag():
    r = GhosttyRenderer(cols=80, rows=24)
    try:
        r.feed(b"\x1b[?1049h")                       # enter alternate screen
        assert r.snapshot().alt_screen is True
    finally:
        r.close()


def test_kitty_keyboard_no_stray_u():
    # nelix-quv was a PYTE artifact (it drew the trailing 'u' of ESC[<u). A faithful engine
    # consumes the kitty-keyboard CSI natively, so NO stray 'u' appears — and the pyte pre-filter
    # is no longer needed. Also verify a sequence split across feed() calls is consumed.
    r = GhosttyRenderer(cols=80, rows=24)
    try:
        r.feed(b"\x1b[H\x1b[<u\x1b[>1u")
        assert not r.render().splitlines()[0].startswith("u")
        r.reset()
        r.feed(b"\x1b[H\x1b[<"); r.feed(b"u\x1b[>1u")  # split across reads
        assert not r.render().splitlines()[0].startswith("u")
    finally:
        r.close()


def test_real_u_text_survives():
    r = GhosttyRenderer(cols=80, rows=24)
    try:
        r.feed(b"menu")
        assert "menu" in r.render()
    finally:
        r.close()


def test_reset_clears():
    r = GhosttyRenderer(cols=40, rows=6)
    try:
        r.feed(b"keep")
        assert "keep" in r.render()
        r.reset()
        assert "keep" not in r.render()
        assert len(r.snapshot().rows) == 6
    finally:
        r.close()


def test_resize_changes_grid_height():
    r = GhosttyRenderer(cols=40, rows=6)
    try:
        r.resize(40, 10)
        assert len(r.snapshot().rows) == 10
    finally:
        r.close()


def test_no_unbounded_memory_across_many_instances():
    # Create/close many renderers; the cached Module must be reused and instances freed.
    for _ in range(200):
        r = GhosttyRenderer(cols=80, rows=24)
        r.feed(b"x")
        r.snapshot()
        r.close()
    gc.collect()                                     # smoke: completes without exhausting memory
