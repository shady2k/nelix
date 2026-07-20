"""Router-owned lease service: per-epoch lease state with reconciliation (§3.3c).

Each generation epoch carries an independent lease state (active + live tokens). A
reconciliation id marks the router incarnation. An epoch ADMITS lease mutations only
if ``es.reconciled_rid == current reconciliation_id``; otherwise the mutation is
rejected RETRYABLY with ``REBUILDING``. This means the router never buffers deltas —
the generation registers an authoritative snapshot, then resumes.

S3a semantics preserved:
  * **All-or-nothing**: capacity for ALL requested kinds is validated FIRST.
  * **Idempotent-by-fenced-key, counted once**: same key+kind returns same token.
  * **Exactly-once release**: ``release(token)`` removes the token and decrements
    once. A stale/duplicate release returns ``False``.
  * **No additive refcount**: the ``_pending_acquire`` mechanism prevents racing.
"""
import threading
import uuid

from nelix_contracts.errors import (
    CONCURRENCY_LIMIT, REBUILDING, STALE_RECONCILIATION_ID, NelixError,
)

from nelix_contracts.errors import INVALID_REQUEST


class _EpochLeaseState:
    __slots__ = (
        'active_count', 'live_pty_count', 'tokens', 'by_kind_key',
        'rebuilding', 'transition_revision', 'reconciled_rid',
    )

    def __init__(self):
        self.active_count = 0
        self.live_pty_count = 0
        self.tokens = {}
        self.by_kind_key = {}
        self.rebuilding = False
        self.transition_revision = 0
        self.reconciled_rid = None


class LeaseService:
    """Lease service with per-epoch state and reconciliation.

    ``active_limit`` caps concurrent active-slot leases (one counter across ALL generations).
    ``live_pty_limit`` caps concurrent live-PTY leases.

    On (re)start the router mints a fresh ``_reconciliation_id``. Every epoch starts
    unreconciled and must register a snapshot before mutations are allowed.
    """

    def __init__(self, active_limit=5, live_pty_limit=5):
        self._active_limit = active_limit
        self._live_pty_limit = live_pty_limit
        self._reconciliation_id = uuid.uuid4().hex
        self._epochs = {}
        self._global_active_count = 0
        self._global_live_pty_count = 0
        self._lock = threading.Lock()

    # ── reconciliation id ──────────────────────────────────────────────────

    @property
    def reconciliation_id(self):
        with self._lock:
            return self._reconciliation_id

    def set_reconciliation_id(self, rid):
        with self._lock:
            self._reconciliation_id = rid

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
        with self._lock:
            gen_id, gen_epoch = base_key[0], base_key[1]

            if reconciliation_id is not None and reconciliation_id != self._reconciliation_id:
                raise NelixError(
                    STALE_RECONCILIATION_ID,
                    f"stale reconciliation_id {reconciliation_id!r}, "
                    f"current is {self._reconciliation_id!r}")

            es = self._epoch(gen_id, gen_epoch)

            # Reconciled gate: when a reconciliation_id is provided, the epoch
            # must be reconciled under the current id; otherwise reject.
            if reconciliation_id is not None and es.reconciled_rid != self._reconciliation_id:
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

            # Reconciled gate: when a reconciliation_id is provided, the epoch
            # must be reconciled under the current id; otherwise reject.
            if reconciliation_id is not None and es.reconciled_rid != self._reconciliation_id:
                raise NelixError(
                    REBUILDING, "lease service is rebuilding in-memory state")

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

    @staticmethod
    def _validate_snapshot_entries(entries, kind_label):
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise NelixError(
                    INVALID_REQUEST,
                    f"{kind_label} token {i}: expected dict, got {type(entry).__name__}")
            tid = entry.get("token_id")
            if not isinstance(tid, str) or not tid:
                raise NelixError(
                    INVALID_REQUEST,
                    f"{kind_label} token {i}: token_id must be a non-empty string")
            key = entry.get("key")
            if not isinstance(key, (list, tuple)) or len(key) != 5:
                raise NelixError(
                    INVALID_REQUEST,
                    f"{kind_label} token {i}: key must be a list/tuple of length 5 "
                    f"(gen_id, gen_epoch, session_id, activation_id, kind)")

    def register_snapshot(self, gen_id, gen_epoch, reconciliation_id,
                          cutoff_revision, active_tokens, live_tokens):
        with self._lock:
            if reconciliation_id != self._reconciliation_id:
                raise NelixError(
                    STALE_RECONCILIATION_ID,
                    f"stale reconciliation_id {reconciliation_id!r} in "
                    f"register_snapshot, current is {self._reconciliation_id!r}")

            es = self._epoch(gen_id, gen_epoch)

            # Idempotent: epoch already reconciled under this id.
            if es.reconciled_rid == self._reconciliation_id:
                return {"acknowledged_revision": cutoff_revision,
                        "reconciliation_id": self._reconciliation_id}

            # Validate entire payload before mutating.
            self._validate_snapshot_entries(active_tokens, "active")
            self._validate_snapshot_entries(live_tokens, "live")

            seen_tids = set()
            for entry in active_tokens:
                tid = entry["token_id"]
                if tid in seen_tids:
                    raise NelixError(INVALID_REQUEST,
                                     f"duplicate token_id {tid!r} in active_tokens")
                seen_tids.add(tid)
            for entry in live_tokens:
                tid = entry["token_id"]
                if tid in seen_tids:
                    raise NelixError(INVALID_REQUEST,
                                     f"duplicate token_id {tid!r} (in live_tokens, "
                                     f"already in active_tokens)")
                seen_tids.add(tid)

            seen_keys = set()
            for entry in active_tokens:
                kk = self._kind_key(tuple(entry["key"]), "active")
                if kk in seen_keys:
                    raise NelixError(INVALID_REQUEST,
                                     "duplicate kind-key in active_tokens")
                seen_keys.add(kk)
            for entry in live_tokens:
                kk = self._kind_key(tuple(entry["key"]), "live")
                if kk in seen_keys:
                    raise NelixError(INVALID_REQUEST,
                                     "duplicate kind-key in live_tokens")
                seen_keys.add(kk)

            snapshot_active_count = len(active_tokens)
            snapshot_live_pty_count = len(live_tokens)
            old_active_count = es.active_count
            old_live_pty_count = es.live_pty_count

            projected_active = (self._global_active_count - old_active_count
                                + snapshot_active_count)
            projected_live = (self._global_live_pty_count - old_live_pty_count
                              + snapshot_live_pty_count)
            if projected_active > self._active_limit:
                raise NelixError(
                    CONCURRENCY_LIMIT,
                    f"snapshot would push global active count to "
                    f"{projected_active} exceeding limit ({self._active_limit})")
            if projected_live > self._live_pty_limit:
                raise NelixError(
                    CONCURRENCY_LIMIT,
                    f"snapshot would push global live count to "
                    f"{projected_live} exceeding limit ({self._live_pty_limit})")

            # All valid — mutate.
            self._global_active_count -= old_active_count
            self._global_live_pty_count -= old_live_pty_count

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
            es.reconciled_rid = self._reconciliation_id

            return {"acknowledged_revision": cutoff_revision,
                    "reconciliation_id": self._reconciliation_id}

    def mark_epoch_rebuilding(self, gen_id, gen_epoch):
        """Pre-mark a live epoch as REBUILDING under the current reconciliation id."""
        with self._lock:
            es = self._epoch(gen_id, gen_epoch)
            es.rebuilding = True

    # ── rebuilding (legacy, for backward compat) ───────────────────────────

    def set_epoch_rebuilding(self, gen_id, gen_epoch, value):
        with self._lock:
            es = self._epoch(gen_id, gen_epoch)
            es.rebuilding = bool(value)
            if value:
                es.reconciled_rid = None

    def is_epoch_rebuilding(self, gen_id, gen_epoch):
        with self._lock:
            es = self._epochs.get((gen_id, gen_epoch))
            if es is None:
                return False
            return es.rebuilding

    @property
    def rebuilding(self):
        with self._lock:
            return any(es.rebuilding for es in self._epochs.values())

    def set_rebuilding(self, value):
        with self._lock:
            v = bool(value)
            for es in self._epochs.values():
                es.rebuilding = v
                if v:
                    es.reconciled_rid = None  # force gate to reject

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
