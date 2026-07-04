"""nelix-kwr: per-executor model-list cache. Keyed by (executor, normalized base_url, auth_kind,
salted token fingerprint) so a token/base_url change never serves a stale-credential list. TTL-bounded
with single-flight coalescing (concurrent cold starts share one fetch). The token itself is never
stored/logged — only a salted sha256 fingerprint is kept."""
import hashlib
import os
import threading
import time


class ModelCache:
    def __init__(self, discover_fn, clock=time.monotonic, ttl=900.0, salt=None):
        self._discover = discover_fn
        self._clock = clock
        self._ttl = ttl
        self._salt = salt if salt is not None else os.urandom(16)
        self._lock = threading.Lock()
        self._entries = {}            # key -> (expires_at, list)
        self._inflight = {}           # key -> threading.Event

    def _key(self, executor, base_url, auth_kind, token):
        fp = hashlib.sha256(self._salt + (token or "").encode()).hexdigest()[:16]
        return (executor, (base_url or "").rstrip("/"), auth_kind, fp)

    def models(self, executor, base_url, auth_kind, token, env, protocol, *, force=False):
        key = self._key(executor, base_url, auth_kind, token)
        while True:
            with self._lock:
                if not force:
                    hit = self._entries.get(key)
                    if hit is not None and hit[0] > self._clock():
                        return hit[1]
                ev = self._inflight.get(key)
                if ev is None:
                    ev = self._inflight[key] = threading.Event()
                    leader = True
                else:
                    leader = False
            if leader:
                try:
                    result = self._discover(protocol, env)
                    with self._lock:
                        self._entries[key] = (self._clock() + self._ttl, result)
                    return result
                finally:
                    with self._lock:
                        self._inflight.pop(key, None)
                    ev.set()
            else:
                ev.wait(self._DISCOVER_WAIT)
                force = False            # a follower re-reads the freshly-populated cache entry

    _DISCOVER_WAIT = 10.0
