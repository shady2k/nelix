"""Router-owned lease service: a single active-slot counter across ALL generations + a
separate global live-PTY bound. Fenced tokens keyed by
``(generation_id, generation_epoch, session_id, activation_id, kind)``; ``acquire`` returns a
dict mapping kind -> opaque token string; ``release`` names the exact token by kind.

Each kind (``"active"``, ``"live"``) gets its own token so partial release is trivial:
release active on idle, release live on terminal.

FIX A semantics:
  * **All-or-nothing**: capacity for ALL requested kinds is validated FIRST. Only if every
    kind can be granted are counters incremented and tokens stored — a single kind at capacity
    raises without mutating anything.
  * **Idempotent-by-fenced-key, counted once**: a second ``acquire`` for the same full key +
    kind returns the SAME token and does NOT touch the global counter. A network retry after
    a lost response is safe; the caller holds exactly one reference and releases once.
  * **Exactly-once release**: ``release(token)`` removes the token and decrements the counter
    exactly once. A stale/duplicate release returns ``False`` without affecting counters.
  * **No additive refcount**: the ``_pending_acquire`` mechanism on the daemon side prevents
    racing double-send_turn from even reaching the router; no shared-token counting is needed.

The router owns this service; a restart loses in-memory state (acceptable for S3a — S3b adds
reconciliation).
"""
import threading
import uuid

from nelix_contracts.errors import CONCURRENCY_LIMIT, REBUILDING, NelixError


class LeaseService:
    """In-memory lease service with two independent bounds.

    ``active_limit`` caps concurrent active-slot leases (one counter across ALL generations).
    ``live_pty_limit`` caps concurrent live-PTY leases (a SEPARATE bound; an idle session
    still holds a PTS/process).
    """

    def __init__(self, active_limit=5, live_pty_limit=5):
        self._active_limit = active_limit
        self._live_pty_limit = live_pty_limit
        self._active_count = 0
        self._live_pty_count = 0
        # token_id -> {"key": kind_key, "kind": str}
        self._tokens = {}
        # kind_key -> {"token_id": str, "kind": str}
        self._by_kind_key = {}
        self._lock = threading.Lock()
        self._rebuilding = False

    def _kind_key(self, base_key, kind):
        return base_key + (kind,)

    def acquire(self, base_key, kinds):
        """Acquire leases for ``kinds`` (iterable of ``"active"`` and/or ``"live"``).

        ``base_key`` is ``(generation_id, generation_epoch, session_id, activation_id)``.

        Returns a dict mapping each kind to ``{"token_id": "...", "fresh": bool}``.
        ``fresh`` is ``True`` when this call created the lease (caller must release on
        rollback), ``False`` when the key was already held (caller must NOT release on
        rollback — the original acquirer will release it).

        Raises ``NelixError`` on capacity or rebuilding.
        """
        with self._lock:
            if self._rebuilding:
                raise NelixError(
                    REBUILDING, "lease service is rebuilding in-memory state")

            # Phase 1: validate ALL kinds against capacity (account for already-held keys).
            needs_active = "active" in kinds
            needs_live = "live" in kinds
            results = {}

            if needs_active:
                kk = self._kind_key(base_key, "active")
                existing = self._by_kind_key.get(kk)
                if existing is not None:
                    results["active"] = {"token_id": existing["token_id"],
                                         "fresh": False}
                    needs_active = False
                elif self._active_count >= self._active_limit:
                    raise NelixError(
                        CONCURRENCY_LIMIT,
                        f"active lease limit ({self._active_limit}) reached")
            if needs_live:
                kk = self._kind_key(base_key, "live")
                existing = self._by_kind_key.get(kk)
                if existing is not None:
                    results["live"] = {"token_id": existing["token_id"],
                                       "fresh": False}
                    needs_live = False
                elif self._live_pty_count >= self._live_pty_limit:
                    raise NelixError(
                        CONCURRENCY_LIMIT,
                        f"live-PTY lease limit ({self._live_pty_limit}) reached")

            # Phase 2: all valid — increment counters and store tokens.
            if needs_active:
                self._active_count += 1
                token_id = uuid.uuid4().hex
                kk = self._kind_key(base_key, "active")
                entry = {"key": kk, "kind": "active", "token_id": token_id}
                self._tokens[token_id] = entry
                self._by_kind_key[kk] = entry
                results["active"] = {"token_id": token_id, "fresh": True}
            if needs_live:
                self._live_pty_count += 1
                token_id = uuid.uuid4().hex
                kk = self._kind_key(base_key, "live")
                entry = {"key": kk, "kind": "live", "token_id": token_id}
                self._tokens[token_id] = entry
                self._by_kind_key[kk] = entry
                results["live"] = {"token_id": token_id, "fresh": True}

            return results

    def release(self, token_id):
        """Release a single token (one kind).

        Returns ``True`` if the token was known and freed, ``False`` if unknown (already
        released or never acquired).
        """
        with self._lock:
            entry = self._tokens.pop(token_id, None)
            if entry is None:
                return False
            self._by_kind_key.pop(entry["key"], None)
            kind = entry.get("kind")
            if kind == "active":
                self._active_count -= 1
            elif kind == "live":
                self._live_pty_count -= 1
            return True

    # ── inspection ────────────────────────────────────────────────────────

    @property
    def active_count(self):
        with self._lock:
            return self._active_count

    @property
    def live_pty_count(self):
        with self._lock:
            return self._live_pty_count

    @property
    def active_limit(self):
        return self._active_limit

    @property
    def live_pty_limit(self):
        return self._live_pty_limit

    @property
    def rebuilding(self):
        with self._lock:
            return self._rebuilding

    def set_rebuilding(self, value):
        with self._lock:
            self._rebuilding = bool(value)

    def token_count(self):
        with self._lock:
            return len(self._tokens)
