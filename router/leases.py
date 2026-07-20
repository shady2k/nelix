"""Router-owned lease service: a single active-slot counter across ALL generations + a
separate global live-PTY bound. Fenced tokens keyed by
``(generation_id, generation_epoch, session_id, activation_id, kind)``; ``acquire`` returns a
dict mapping kind -> opaque token string; ``release`` names the exact token by kind.

Each kind (``"active"``, ``"live"``) gets its own token so partial release is trivial:
release active on idle, release live on terminal. Acquire is idempotent per-kind-key: a
duplicate acquire for the same key returns the same grant without double-counting capacity.

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

    ``acquire()`` returns ``{"active": "<token>", "live": "<token>"}`` for the requested kinds.
    ``release(token_id)`` frees the slot associated with that kind.

    Idempotency is per-kind-key: the key for a kind is the base key tuple plus the kind string.
    A second acquire with the same four-part key + kind increments a refcount and returns the
    same token_id without touching the global counters. The last release (refcount -> 0) drops
    the slot from the counter.
    """

    def __init__(self, active_limit=5, live_pty_limit=5):
        self._active_limit = active_limit
        self._live_pty_limit = live_pty_limit
        self._active_count = 0
        self._live_pty_count = 0
        # token_id -> {key, refcount}
        self._tokens = {}
        # kind-key -> token_id (for idempotency)
        self._by_kind_key = {}
        self._lock = threading.Lock()
        self._rebuilding = False

    def _kind_key(self, base_key, kind):
        return base_key + (kind,)

    def acquire(self, base_key, kinds):
        """Acquire leases for ``kinds`` (iterable of ``"active"`` and/or ``"live"``).

        ``base_key`` is ``(generation_id, generation_epoch, session_id, activation_id)``.

        Returns a dict mapping each kind to its opaque token string, or raises NelixError.

        Idempotent by per-kind-key: a concurrent acquire with the same key + kind returns the
        same token without double-counting. The caller must call ``release`` for each token
        exactly once per returned reference (the last release frees the slot).
        """
        with self._lock:
            if self._rebuilding:
                raise NelixError(
                    REBUILDING, "lease service is rebuilding in-memory state")

            result = {}
            for kind in kinds:
                kk = self._kind_key(base_key, kind)
                existing = self._by_kind_key.get(kk)
                if existing is not None:
                    existing["refcount"] += 1
                    result[kind] = existing["token_id"]
                    continue

                if kind == "active":
                    if self._active_count >= self._active_limit:
                        raise NelixError(
                            CONCURRENCY_LIMIT,
                            f"active lease limit ({self._active_limit}) reached")
                    self._active_count += 1
                elif kind == "live":
                    if self._live_pty_count >= self._live_pty_limit:
                        raise NelixError(
                            CONCURRENCY_LIMIT,
                            f"live-PTY lease limit ({self._live_pty_limit}) reached")
                    self._live_pty_count += 1
                else:
                    continue

                token_id = uuid.uuid4().hex
                entry = {"key": kk, "refcount": 1, "token_id": token_id, "kind": kind}
                self._tokens[token_id] = entry
                self._by_kind_key[kk] = entry
                result[kind] = token_id

            return result

    def release(self, token_id):
        """Release a single token (one kind).

        The token is freed (and its slot decremented) only when the refcount reaches zero,
        i.e. when every concurrent holder of this token has called release.

        Returns ``True`` if the token was known and freed/released, ``False`` if unknown.
        """
        with self._lock:
            entry = self._tokens.get(token_id)
            if entry is None:
                return False

            entry["refcount"] -= 1
            if entry["refcount"] <= 0:
                self._tokens.pop(token_id, None)
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
