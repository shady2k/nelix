"""Per-session wake registry: one cursor + arm-dedup per active session, replacing the single
global CursorState. Each session re-arms its own waiter independently by its own seq, so the
old global-cursor concerns (burst-collapse, answer-B-skips-A, out-of-order) dissolve.

Thread-safe: every operation is under one lock. claim_arm() makes the check-and-mark atomic so
two concurrent status reconciles for the same session spawn exactly one waiter."""
import threading

_UNSET = object()


class WakeRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._value = {}        # sid -> observed cursor (seq we've seen up to)
        self._armed_at = {}     # sid -> the value a waiter is currently out for
        self._daemon_id = None  # identity (pid) of the daemon these sessions belong to

    def _reset_if_new_daemon(self, daemon_id):
        # caller holds self._lock. A daemon pid change means every old session is gone (new
        # daemon restarts seq at ~0); clear so stale sids never re-arm and new ones start fresh.
        if daemon_id is not None and daemon_id != self._daemon_id:
            self._daemon_id = daemon_id
            self._value.clear()
            self._armed_at.clear()

    def on_start(self, sid, base_seq, daemon_id=None):
        with self._lock:
            self._reset_if_new_daemon(daemon_id)
            self._value[sid] = base_seq

    def on_status(self, sid, seq):
        with self._lock:
            self._value[sid] = seq

    def on_respond(self, sid, next_after_seq):
        with self._lock:
            self._value[sid] = next_after_seq

    def drop(self, sid):
        with self._lock:
            self._value.pop(sid, None)
            self._armed_at.pop(sid, None)

    def claim_arm(self, sid):
        """Atomic: if `sid` needs a waiter (cursor differs from last-armed), mark it armed and
        return the after_seq to arm at; else None. The caller dispatches the waiter OUTSIDE the
        lock. Marking under the same lock as the check prevents a double-spawn under concurrency."""
        with self._lock:
            v = self._value.get(sid, _UNSET)
            if v is _UNSET:
                return None                      # unknown/dropped session — nothing to arm
            if self._armed_at.get(sid, _UNSET) == v:
                return None                      # a waiter is already out for this cursor value
            self._armed_at[sid] = v
            return v

    # ---- introspection (tests / reconcile) ----
    def value(self, sid):
        with self._lock:
            v = self._value.get(sid, _UNSET)
            return None if v is _UNSET else v

    def should_arm(self, sid):
        with self._lock:
            v = self._value.get(sid, _UNSET)
            return v is not _UNSET and self._armed_at.get(sid, _UNSET) != v

    def active_sids(self):
        with self._lock:
            return set(self._value)
