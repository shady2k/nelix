from daemon.renderer.ghostty import GhosttyRenderer
from daemon.drivers.claude import ClaudeDriver

def replay_frames(raw, *, cols=120, rows=40, chunk=256):
    r = GhosttyRenderer(cols, rows); seen = None
    try:
        for i in range(0, len(raw), chunk):
            r.feed(raw[i:i + chunk]); frame = r.render()
            if frame == seen: continue
            seen = frame; yield i, frame
    finally:
        r.close()

def replay_observations(raw, ctx, *, cols=120, rows=40, chunk=256, drv=None):
    drv = drv or ClaudeDriver()
    for off, frame in replay_frames(raw, cols=cols, rows=rows, chunk=chunk):
        yield off, drv.observe(frame, ctx)
