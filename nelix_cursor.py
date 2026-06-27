"""Plugin-held global wake cursor (observed_cursor) + single-waiter arm-dedup. One per plugin process.

Invariant: arm the single global waiter from `value` only; advance `value` ONLY on a full-board
status read; never from a per-session start/respond seq. This makes a missed wake impossible
(status is the source of truth) and is safe across out-of-order answers and start bursts.

Single-waiter: `should_arm()` is True only when `value` differs from the last-armed value, so a BURST
of starts (which leave `value` put) arms exactly one waiter; after a real wake the companion's status
read advances `value`, so the next arm fires. The long-poll waiter is one-shot-per-event and only exits
on an event or daemon-gone, so 'a waiter is out for this value' stays true until the next event."""

_UNSET = object()


class CursorState:
    def __init__(self):
        self.value = None          # None until the first start of a (re)started daemon
        self._armed_at = _UNSET    # the `value` we last armed a waiter at (sentinel => never armed)
        self._daemon_id = None     # identity (pid) of the daemon the cursor currently tracks

    def on_start(self, base_seq, daemon_id=None):
        # Reset on a DAEMON CHANGE (pid differs), not on a seq heuristic: a fresh daemon restarts
        # its seq at ~0, so two daemons can both report base_seq=0 — a seq-only rule would then fail
        # to re-arm (value and _armed_at both 0). Keying on daemon_id makes the reset reliable and
        # clears the arm-dedup so the new daemon always gets a waiter.
        if daemon_id is not None and daemon_id != self._daemon_id:
            self._daemon_id = daemon_id
            self.value = base_seq
            self._armed_at = _UNSET
            return
        # Same daemon (or unknown id): keep the lowest unobserved baseline; a burst (base_seq >=
        # value) leaves it put -> no skip. Fallback `base_seq < value` reset covers a None daemon_id.
        if self.value is None or base_seq < self.value:
            self.value = base_seq

    def on_status(self, cursor):
        # The only place the cursor advances: the companion has now SEEN the board up to `cursor`.
        if cursor is not None:
            self.value = cursor

    def on_respond(self, next_after_seq=None):
        # Intentionally a no-op for the cursor: per-session respond seqs must never drive the
        # global waiter (answering B before A would otherwise skip A's wake).
        return

    def arm_after(self):
        return self.value if self.value is not None else 0

    def should_arm(self):
        # Arm only if no waiter is already out for the current cursor value (collapses a start burst
        # to one waiter). After on_status advances `value`, this is True again -> re-arm.
        return self._armed_at is _UNSET or self._armed_at != self.value

    def mark_armed(self):
        self._armed_at = self.value
