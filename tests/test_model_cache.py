import threading
import pytest
from daemon.model_cache import ModelCache

LIST = [{"id": "glm-5.2", "display_name": "GLM-5.2"}]


def _cache(calls, clock=None, ttl=900.0):
    def disc(env):
        calls.append(env)
        return LIST
    return ModelCache(disc, clock=clock or (lambda: 0.0), ttl=ttl)


def test_hit_avoids_second_fetch():
    calls = []
    c = _cache(calls)
    a = c.models("zai", "https://b/", "bearer", "tok", {})
    b = c.models("zai", "https://b/", "bearer", "tok", {})
    assert a == b == LIST
    assert len(calls) == 1                       # second served from cache


def test_force_refetches():
    calls = []
    c = _cache(calls)
    c.models("zai", "https://b", "bearer", "tok", {})
    c.models("zai", "https://b", "bearer", "tok", {}, force=True)
    assert len(calls) == 2


def test_ttl_expiry_refetches():
    calls = []
    now = [0.0]
    c = _cache(calls, clock=lambda: now[0], ttl=10.0)
    c.models("zai", "https://b", "bearer", "tok", {})
    now[0] = 11.0
    c.models("zai", "https://b", "bearer", "tok", {})
    assert len(calls) == 2


def test_different_token_is_different_key():
    calls = []
    c = _cache(calls)
    c.models("zai", "https://b", "bearer", "tokA", {})
    c.models("zai", "https://b", "bearer", "tokB", {})   # visibility is credential-scoped
    assert len(calls) == 2


def test_base_url_trailing_slash_is_same_key():
    calls = []
    c = _cache(calls)
    c.models("zai", "https://b/", "bearer", "tok", {})
    c.models("zai", "https://b", "bearer", "tok", {})
    assert len(calls) == 1


def test_single_flight_coalesces_concurrent_cold_calls():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def disc(env):
        calls.append(1)
        started.set()
        release.wait(2)
        return LIST

    c = ModelCache(disc, clock=lambda: 0.0)
    outs = []
    def worker():
        outs.append(c.models("zai", "https://b", "bearer", "tok", {}))
    ts = [threading.Thread(target=worker) for _ in range(5)]
    for t in ts: t.start()
    started.wait(2)
    release.set()
    for t in ts: t.join(2)
    assert outs == [LIST] * 5
    assert len(calls) == 1               # 5 concurrent cold calls -> exactly one fetch
