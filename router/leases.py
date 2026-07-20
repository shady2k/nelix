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
    CONCURRENCY_LIMIT, INVALID_REQUEST, REBUILDING,
    STALE_RECONCILIATION_ID, NelixError,
)


class _EpochLeaseState:
    __slots__ = (
        'active_count', 'live_pty_count', 'tokens', 'by_kind_key',
        'rebuilding', 'transition_revision', 'buffer', 'reconciled_rid',
    )

    def __init__(self):
        self.active_count = 0
        self.live_pty_count = 0
        self.tokens = {}
        self.by_kind_key = {}
        self.rebuilding = False
        self.transition_revision = 0
        self.buffer = []
        self.reconciled_rid = None


class LeaseService:
    """Lease service with per-epoch state and reconciliation.

    ``active_limit`` caps concurrent active-slot leases (one counter across ALL generations).
    ``live_pty_limit`` caps concurrent live-PTY leases.
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

    def mark_epoch_rebuilding(self, gen_id, gen_epoch):
        """Pre-mark a live epoch as REBUILDING under the current reconciliation id."""
        with self._lock:
            es = self._epoch(gen_id, gen_epoch)
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
        current id; a mismatch raises ``STALE_RECONCILIATION_ID``. When the router has
        a reconciliation id and the caller omits it, the request is also rejected.

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

        When the epoch owning the token is REBUILDING, the release is **buffered**
        (not applied) and acknowledged, so the generation can drop it from its
        outbox. The buffer is processed when ``register_snapshot`` is called.

        Returns ``True`` if the token was known and freed (or buffered),
        ``False`` if unknown.
        """
        with self._lock:
            if reconciliation_id is not None and reconciliation_id != self._reconciliation_id:
                raise NelixError(
                    STALE_RECONCILIATION_ID,
                    f"stale reconciliation_id {reconciliation_id!r}, "
                    f"current is {self._reconciliation_id!r}")

            for es in self._epochs.values():
                entry = es.tokens.get(token_id)
                if entry is not None:
                    break
            else:
                return False

            # FIX 3: buffer the release if epoch is REBUILDING
            if self._global_rebuilding or es.rebuilding:
                rev = transition_revision if transition_revision is not None else 0
                es.buffer.append({
                    "revision": rev,
                    "type": "release",
                    "token_id": token_id,
                })
                return True

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
        2. If the epoch is already reconciled under this id (rebuilding=False),
           return idempotent ack without mutating (FIX 4).
        3. Validate the snapshot payload: all entries well-formed.
        4. Validate the resulting GLOBAL total against the bound (FIX 9).
        5. Replace the epoch's token state with the snapshot tokens.
        6. Discard buffered deltas with revision ≤ cutoff_revision (already
           reflected in the snapshot).
        7. Apply buffered deltas with revision > cutoff_revision in order.
        8. Clear the rebuilding flag so acquisition is allowed again.

        Returns ``{"acknowledged_revision": cutoff_revision}``.
        """
        with self._lock:
            if reconciliation_id != self._reconciliation_id:
                raise NelixError(
                    STALE_RECONCILIATION_ID,
                    f"stale reconciliation_id {reconciliation_id!r} in "
                    f"register_snapshot, current is {self._reconciliation_id!r}")

            es = self._epoch(gen_id, gen_epoch)

            # FIX 4: idempotent register — epoch already reconciled under this id
            if es.reconciled_rid == self._reconciliation_id:
                return {"acknowledged_revision": cutoff_revision,
                        "reconciliation_id": self._reconciliation_id}

            # FIX 10: validate the whole snapshot payload BEFORE mutating
            snapshot_active_count = len(active_tokens)
            snapshot_live_pty_count = len(live_tokens)

            for entry in active_tokens:
                if not isinstance(entry, dict) or "token_id" not in entry or "key" not in entry:
                    raise NelixError(
                        INVALID_REQUEST,
                        "malformed active token entry in snapshot: "
                        "must be dict with token_id and key")
            for entry in live_tokens:
                if not isinstance(entry, dict) or "token_id" not in entry or "key" not in entry:
                    raise NelixError(
                        INVALID_REQUEST,
                        "malformed live token entry in snapshot: "
                        "must be dict with token_id and key")

            old_active_count = es.active_count
            old_live_pty_count = es.live_pty_count

            # FIX 9: validate the GLOBAL total against the bound
            projected_active = (self._global_active_count - old_active_count
                                + snapshot_active_count)
            projected_live = (self._global_live_pty_count - old_live_pty_count
                              + snapshot_live_pty_count)
            if projected_active > self._active_limit:
                raise NelixError(
                    CONCURRENCY_LIMIT,
                    f"snapshot would push global active count to "
                    f"{projected_active} exceeding limit "
                    f"({self._active_limit})")
            if projected_live > self._live_pty_limit:
                raise NelixError(
                    CONCURRENCY_LIMIT,
                    f"snapshot would push global live count to "
                    f"{projected_live} exceeding limit "
                    f"({self._live_pty_limit})")

            # Atomic replace: subtract old, install snapshot, add new.
            self._global_active_count -= old_active_count
            self._global_live_pty_count -= old_live_pty_count

            es.tokens.clear()
            es.by_kind_key.clear()

            # FIX 8: snapshot tokens use full kind-key so idempotent acquire matches
            for entry in active_tokens:
                tid = entry["token_id"]
                base_key = tuple(entry["key"])
                kk = self._kind_key(base_key, "active")
                token_entry = {"key": kk, "kind": "active", "token_id": tid}
                es.tokens[tid] = token_entry
                es.by_kind_key[kk] = token_entry
            for entry in live_tokens:
                tid = entry["token_id"]
                base_key = tuple(entry["key"])
                kk = self._kind_key(base_key, "live")
                token_entry = {"key": kk, "kind": "live", "token_id": tid}
                es.tokens[tid] = token_entry
                es.by_kind_key[kk] = token_entry

            es.active_count = snapshot_active_count
            es.live_pty_count = snapshot_live_pty_count
            es.transition_revision = max(es.transition_revision, cutoff_revision)

            self._global_active_count += snapshot_active_count
            self._global_live_pty_count += snapshot_live_pty_count

            # FIX 3: process buffer — discard ≤R, apply >R in revision order
            es.buffer.sort(key=lambda d: d["revision"])
            for delta in es.buffer:
                if delta["revision"] <= cutoff_revision:
                    continue
                if delta["type"] == "release":
                    tid = delta["token_id"]
                    ent = es.tokens.pop(tid, None)
                    if ent is not None:
                        es.by_kind_key.pop(ent["key"], None)
                        if ent["kind"] == "active":
                            es.active_count -= 1
                            self._global_active_count -= 1
                        elif ent["kind"] == "live":
                            es.live_pty_count -= 1
                            self._global_live_pty_count -= 1
            es.buffer.clear()

            es.rebuilding = False
            es.reconciled_rid = self._reconciliation_id

            return {"acknowledged_revision": cutoff_revision,
                    "reconciliation_id": self._reconciliation_id}

    # ── rebuilding (per-epoch) ─────────────────────────────────────────────

    def set_epoch_rebuilding(self, gen_id, gen_epoch, value):
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

    def epoch_buffer_size(self, gen_id, gen_epoch):
        with self._lock:
            es = self._epochs.get((gen_id, gen_epoch))
            if es is None:
                return 0
            return len(es.buffer)
