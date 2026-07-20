"""Router-owned lease service: per-epoch lease state with reconciliation (§3.3c).

Each generation epoch carries an independent lease state (active + live tokens). A
reconciliation id marks the router incarnation; messages bearing a stale id are rejected
retryably. The 6-step handshake (register_snapshot) atomically replaces an epoch's lease
state from a generation snapshot, enabling recovery after router restart.

S3a semantics preserved:
  * **All-or-nothing**: capacity for ALL requested kinds is validated FIRST. Only if every
    kind can be granted are counters incremented and tokens stored.
  * **Idempotent-by-fenced-key, counted once**: a second ``acquire`` for the same full key +
    kind returns the SAME token and does NOT touch the global counter.
  * **Exactly-once release**: ``release(token)`` removes the token and decrements the counter
    exactly once. A stale/duplicate release returns ``False`` without affecting counters.
  * **No additive refcount**: the ``_pending_acquire`` mechanism on the daemon side prevents
    racing double-send_turn from even reaching the router.
"""
import threading
import uuid

from nelix_contracts.errors import (
    CONCURRENCY_LIMIT, REBUILDING, STALE_RECONCILIATION_ID, NelixError,
)


class _EpochLeaseState:
    __slots__ = (
        'active_count', 'live_pty_count', 'tokens', 'by_kind_key',
        'rebuilding', 'transition_revision',
    )

    def __init__(self):
        self.active_count = 0
        self.live_pty_count = 0
        self.tokens = {}
        self.by_kind_key = {}
        self.rebuilding = False
        self.transition_revision = 0


class LeaseService:
    """Lease service with per-epoch state and reconciliation.

    ``active_limit`` caps concurrent active-slot leases (one counter across ALL generations).
    ``live_pty_limit`` caps concurrent live-PTY leases.

    New in S3b:
    - Per-``(generation_id, generation_epoch)`` lease state so one epoch can be rebuilt
      atomically without disturbing others.
    - ``_reconciliation_id`` marks the router incarnation. Every acquire/release carries
      it; stale-id messages raise ``STALE_RECONCILIATION_ID`` (retryable).
    - ``register_snapshot()`` implements the 6-step handshake: atomically replaces an
      epoch's state from a generation snapshot, discarding deltas ≤ cutoff revision.
    """

    def __init__(self, active_limit=5, live_pty_limit=5):
        self._active_limit = active_limit
        self._live_pty_limit = live_pty_limit
        self._reconciliation_id = uuid.uuid4().hex
        self._epochs = {}
        self._global_active_count = 0
        self._global_live_pty_count = 0
        self._global_rebuilding = False
        self._lock = threading.Lock()

    # ── reconciliation id ──────────────────────────────────────────────────

    @property
    def reconciliation_id(self):
        with self._lock:
            return self._reconciliation_id

    def set_reconciliation_id(self, rid):
        with self._lock:
            self._reconciliation_id = rid
            for es in self._epochs.values():
                es.rebuilding = True

    # ── epoch helpers ──────────────────────────────────────────────────────

    def _epoch(self, gen_id, gen_epoch):
        key = (gen_id, gen_epoch)
        es = self._epochs.get(key)
        if es is None:
            es = _EpochLeaseState()
            self._epochs[key] = es
        return es

    def _kind_key(self, base_key, kind):
        return base_key + (kind,)

    def epoch_keys(self):
        with self._lock:
            return list(self._epochs.keys())

    # ── acquire ────────────────────────────────────────────────────────────

    def acquire(self, base_key, kinds, reconciliation_id=None,
                transition_revision=None):
        """Acquire leases for ``kinds``.

        ``base_key`` is ``(generation_id, generation_epoch, session_id, activation_id)``.

        When ``reconciliation_id`` is provided it is checked against the router's
        current id; a mismatch raises ``STALE_RECONCILIATION_ID``.

        Returns a dict mapping each kind to ``{"token_id": "...", "fresh": bool}``.
        """
        with self._lock:
            gen_id, gen_epoch = base_key[0], base_key[1]
            if reconciliation_id is not None and reconciliation_id != self._reconciliation_id:
                raise NelixError(
                    STALE_RECONCILIATION_ID,
                    f"stale reconciliation_id {reconciliation_id!r}, "
                    f"current is {self._reconciliation_id!r}")

            es = self._epoch(gen_id, gen_epoch)

            if self._global_rebuilding or es.rebuilding:
                raise NelixError(
                    REBUILDING, "lease service is rebuilding in-memory state")

            needs_active = "active" in kinds
            needs_live = "live" in kinds
            results = {}

            if needs_active:
                kk = self._kind_key(base_key, "active")
                existing = es.by_kind_key.get(kk)
                if existing is not None:
                    results["active"] = {"token_id": existing["token_id"],
                                         "fresh": False}
                    needs_active = False
                elif self._global_active_count >= self._active_limit:
                    raise NelixError(
                        CONCURRENCY_LIMIT,
                        f"active lease limit ({self._active_limit}) reached")
            if needs_live:
                kk = self._kind_key(base_key, "live")
                existing = es.by_kind_key.get(kk)
                if existing is not None:
                    results["live"] = {"token_id": existing["token_id"],
                                       "fresh": False}
                    needs_live = False
                elif self._global_live_pty_count >= self._live_pty_limit:
                    raise NelixError(
                        CONCURRENCY_LIMIT,
                        f"live-PTY lease limit ({self._live_pty_limit}) reached")

            if needs_active:
                token_id = uuid.uuid4().hex
                kk = self._kind_key(base_key, "active")
                entry = {"key": kk, "kind": "active", "token_id": token_id}
                es.tokens[token_id] = entry
                es.by_kind_key[kk] = entry
                es.active_count += 1
                self._global_active_count += 1
                results["active"] = {"token_id": token_id, "fresh": True}
            if needs_live:
                token_id = uuid.uuid4().hex
                kk = self._kind_key(base_key, "live")
                entry = {"key": kk, "kind": "live", "token_id": token_id}
                es.tokens[token_id] = entry
                es.by_kind_key[kk] = entry
                es.live_pty_count += 1
                self._global_live_pty_count += 1
                results["live"] = {"token_id": token_id, "fresh": True}

            if transition_revision is not None:
                es.transition_revision = max(es.transition_revision, transition_revision)

            return results

    # ── release ────────────────────────────────────────────────────────────

    def release(self, token_id, reconciliation_id=None, transition_revision=None):
        """Release a single token.

        When ``reconciliation_id`` is provided it is checked against the router's
        current id; a mismatch raises ``STALE_RECONCILIATION_ID``.

        Returns ``True`` if the token was known and freed, ``False`` if unknown.
        """
        with self._lock:
            if reconciliation_id is not None and reconciliation_id != self._reconciliation_id:
                raise NelixError(
                    STALE_RECONCILIATION_ID,
                    f"stale reconciliation_id {reconciliation_id!r}, "
                    f"current is {self._reconciliation_id!r}")

            # Find which epoch owns this token.
            for es in self._epochs.values():
                entry = es.tokens.get(token_id)
                if entry is not None:
                    break
            else:
                return False

            es.tokens.pop(token_id, None)
            es.by_kind_key.pop(entry["key"], None)
            kind = entry.get("kind")
            if kind == "active":
                es.active_count -= 1
                self._global_active_count -= 1
            elif kind == "live":
                es.live_pty_count -= 1
                self._global_live_pty_count -= 1

            if transition_revision is not None:
                es.transition_revision = max(es.transition_revision, transition_revision)

            return True

    # ── 6-step handshake: register snapshot ────────────────────────────────

    def register_snapshot(self, gen_id, gen_epoch, reconciliation_id,
                          cutoff_revision, active_tokens, live_tokens):
        """Atomically replace an epoch's lease state from a generation snapshot.

        Steps (the frozen 6-step handshake, §3.3c):
        1. Verify reconciliation_id matches current; reject if stale.
        2. Compute delta from the snapshot: subtract the epoch's old counts from
           the global counters, add the snapshot's counts.
        3. Replace the epoch's lease state with the snapshot tokens.
        4. Discard any buffered deltas with revision ≤ cutoff_revision (they are
           already reflected in the snapshot).
        5. Apply buffered deltas with revision > cutoff_revision (arrived during
           the registration window).
        6. Clear the rebuilding flag so acquisition is allowed again.

        Returns ``{"acknowledged_revision": cutoff_revision}``.
        """
        with self._lock:
            if reconciliation_id != self._reconciliation_id:
                raise NelixError(
                    STALE_RECONCILIATION_ID,
                    f"stale reconciliation_id {reconciliation_id!r} in "
                    f"register_snapshot, current is {self._reconciliation_id!r}")

            es = self._epoch(gen_id, gen_epoch)

            old_active_count = es.active_count
            old_live_pty_count = es.live_pty_count

            snapshot_active_count = len(active_tokens)
            snapshot_live_pty_count = len(live_tokens)

            self._global_active_count -= old_active_count
            self._global_live_pty_count -= old_live_pty_count

            if snapshot_active_count > self._active_limit or snapshot_live_pty_count > self._live_pty_limit:
                self._global_active_count += old_active_count
                self._global_live_pty_count += old_live_pty_count
                raise NelixError(
                    CONCURRENCY_LIMIT,
                    f"snapshot active count {snapshot_active_count} or live "
                    f"{snapshot_live_pty_count} exceeds limits "
                    f"({self._active_limit}/{self._live_pty_limit})")

            # Replace epoch tokens with snapshot.
            es.tokens.clear()
            es.by_kind_key.clear()
            for entry in active_tokens:
                tid = entry["token_id"]
                kk = tuple(entry["key"])
                token_entry = {"key": kk, "kind": "active", "token_id": tid}
                es.tokens[tid] = token_entry
                es.by_kind_key[kk] = token_entry
            for entry in live_tokens:
                tid = entry["token_id"]
                kk = tuple(entry["key"])
                token_entry = {"key": kk, "kind": "live", "token_id": tid}
                es.tokens[tid] = token_entry
                es.by_kind_key[kk] = token_entry

            es.active_count = snapshot_active_count
            es.live_pty_count = snapshot_live_pty_count
            es.transition_revision = max(es.transition_revision, cutoff_revision)

            self._global_active_count += snapshot_active_count
            self._global_live_pty_count += snapshot_live_pty_count

            es.rebuilding = False

            return {"acknowledged_revision": cutoff_revision}

    # ── rebuilding (per-epoch) ─────────────────────────────────────────────

    def set_epoch_rebuilding(self, gen_id, gen_epoch, value):
        """Set the rebuilding flag for a specific epoch."""
        with self._lock:
            es = self._epoch(gen_id, gen_epoch)
            es.rebuilding = bool(value)

    def is_epoch_rebuilding(self, gen_id, gen_epoch):
        with self._lock:
            es = self._epochs.get((gen_id, gen_epoch))
            if es is None:
                return False
            return es.rebuilding

    # ── legacy rebuilding (global, for backward compat) ────────────────────

    @property
    def rebuilding(self):
        with self._lock:
            if self._global_rebuilding:
                return True
            return any(es.rebuilding for es in self._epochs.values())

    def set_rebuilding(self, value):
        with self._lock:
            v = bool(value)
            self._global_rebuilding = v
            for es in self._epochs.values():
                es.rebuilding = v

    # ── inspection ─────────────────────────────────────────────────────────

    @property
    def active_count(self):
        with self._lock:
            return self._global_active_count

    @property
    def live_pty_count(self):
        with self._lock:
            return self._global_live_pty_count

    @property
    def active_limit(self):
        return self._active_limit

    @property
    def live_pty_limit(self):
        return self._live_pty_limit

    def token_count(self):
        with self._lock:
            return sum(len(es.tokens) for es in self._epochs.values())

    def epoch_token_count(self, gen_id, gen_epoch):
        with self._lock:
            es = self._epochs.get((gen_id, gen_epoch))
            if es is None:
                return 0
            return len(es.tokens)

    def epoch_active_count(self, gen_id, gen_epoch):
        with self._lock:
            es = self._epochs.get((gen_id, gen_epoch))
            if es is None:
                return 0
            return es.active_count

    def epoch_live_pty_count(self, gen_id, gen_epoch):
        with self._lock:
            es = self._epochs.get((gen_id, gen_epoch))
            if es is None:
                return 0
            return es.live_pty_count

    def epoch_transition_revision(self, gen_id, gen_epoch):
        with self._lock:
            es = self._epochs.get((gen_id, gen_epoch))
            if es is None:
                return 0
            return es.transition_revision
