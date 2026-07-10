import re

_WS = re.compile(r"\s+")


def _norm(s):
    return _WS.sub(" ", s.strip())


class _Obj:
    __slots__ = ("norm", "text", "y", "first_seen", "last_seen", "seen", "committed")

    def __init__(self, norm, text, y, frame_no):
        self.norm = norm
        self.text = text
        self.y = y
        self.first_seen = frame_no
        self.last_seen = frame_no
        self.seen = 1
        self.committed = False


class TranscriptBuilder:
    """Reconstruct the dialog transcript from the faithful frame stream: commit a content line
    exactly once, when it has been stably on screen and then scrolls out of the viewport. Tracks
    OCCURRENCES (text + nearby row position), so legitimate repeats are preserved. Single-threaded:
    the owning PtySession feeds it on the monitor thread only."""

    def __init__(self, dialog, driver, rows, *, stable=2, grace=4, match_window=3):
        self._dialog = dialog
        self._driver = driver
        self._rows = rows
        self._stable = stable
        self._grace = grace
        self._mw = match_window
        self._frame_no = 0
        self._tracked = []           # list[_Obj] — content lines believed on screen now

    def _content_rows(self, rows):
        # The committable content lines of a frame (non-blank, non-chrome), with PARTIAL-REDRAW
        # REMNANTS dropped. When a choice modal / prompt paints over streamed content from the TOP
        # DOWN, one captured frame is TORN: a line already shown above re-appears LOWER as a stale
        # leftover the repaint has not overwritten yet — separated from the real occurrence by the
        # active-prompt CHROME (its ❯ input / option-select / spinner rows). Committing that torn frame
        # would append the line twice (regression s-9610d25c: the "…do you want me to act now?" tail
        # committed as lines 67-86 then again 87-102). Such a lower duplicate is dropped, keeping the
        # TOPMOST (real) occurrence. The discriminator is deliberately narrow: an EXACT (whitespace-
        # sensitive) repeat with PROMPT chrome between the two occurrences. `is_prompt_chrome` (when
        # the driver provides it) EXCLUDES bare rule/separator rows (═══, box borders) so a genuine
        # repeated content line across a markdown/table separator is NOT dropped; adjacent identical
        # rows and repeats separated only by other CONTENT have no prompt chrome between them and are
        # kept; cross-frame / cross-turn repeats never enter one frame's window at all.
        is_chrome = getattr(self._driver, "is_prompt_chrome", self._driver.is_transcript_volatile)
        skip = [bool(r.strip()) and self._driver.is_transcript_volatile(r) for r in rows]
        chrome = [bool(r.strip()) and is_chrome(r) for r in rows]
        out = []
        first_y = {}                                   # exact text -> y of its first kept occurrence
        for y, row in enumerate(rows):
            if not row.strip() or skip[y]:
                continue
            text = row.rstrip()
            prev = first_y.get(text)
            if prev is not None and any(chrome[prev + 1:y]):
                continue                               # redraw remnant below an active-prompt boundary
            out.append((y, row))
            first_y.setdefault(text, y)
        return out

    def observe(self, frame):
        fno = self._frame_no
        content = self._content_rows(frame.rows)
        unmatched = set(range(len(self._tracked)))     # indices into the pre-frame _tracked prefix
        for y, row in content:
            n = _norm(row)
            best = None
            best_d = None
            for i in unmatched:
                o = self._tracked[i]
                if o.norm == n:
                    d = abs(o.y - y)
                    if d <= self._mw and (best_d is None or d < best_d):
                        best, best_d = i, d
            if best is not None:
                o = self._tracked[best]
                o.last_seen = fno
                o.seen += 1
                o.y = y
                unmatched.discard(best)
            else:
                self._tracked.append(_Obj(n, row.rstrip(), y, fno))
        survivors = []
        to_commit = []
        for o in self._tracked:
            if o.last_seen <= fno - self._grace:        # gone for >= grace frames
                if o.seen >= self._stable and not o.committed:
                    to_commit.append(o)
                # else: transient (seen < stable) or already committed -> drop
            else:
                survivors.append(o)
        for o in sorted(to_commit, key=lambda o: (o.first_seen, o.y)):
            self._dialog.add_agent_line(o.text)
            o.committed = True
        self._tracked = survivors
        self._frame_no = fno + 1

    def finalize(self, frame=None):
        # Commit the visible tail at a stop: EVERY still-tracked uncommitted line, regardless of
        # `seen` (the last screen / a no-ESU executor's content may have been seen only once).
        if frame is not None:
            self.observe(frame)
        pending = [o for o in self._tracked if not o.committed]
        for o in sorted(pending, key=lambda o: (o.first_seen, o.y)):
            self._dialog.add_agent_line(o.text)
            o.committed = True
