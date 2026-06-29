import threading
from nelix_cursor import WakeRegistry


def test_on_start_sets_value_and_claims_one_waiter():
    r = WakeRegistry()
    r.on_start("s-a", base_seq=3, daemon_id=1)
    assert r.value("s-a") == 3
    assert r.claim_arm("s-a") == 3        # first claim arms at base
    assert r.claim_arm("s-a") is None     # already armed for value 3 -> no second waiter


def test_after_seq_zero_is_not_skipped():
    r = WakeRegistry()
    r.on_start("s-a", base_seq=0, daemon_id=1)
    assert r.claim_arm("s-a") == 0        # 0 is a valid after_seq, must arm (not falsy-skipped)


def test_status_advance_rearms():
    r = WakeRegistry()
    r.on_start("s-a", 0, daemon_id=1)
    assert r.claim_arm("s-a") == 0
    r.on_status("s-a", 7)
    assert r.claim_arm("s-a") == 7        # cursor advanced -> re-arm
    assert r.claim_arm("s-a") is None


def test_respond_advances_per_session_cursor():
    r = WakeRegistry()
    r.on_start("s-a", 0, daemon_id=1)
    r.claim_arm("s-a")
    r.on_respond("s-a", 5)                # per-session advance is correct (no cross-session skip)
    assert r.value("s-a") == 5
    assert r.claim_arm("s-a") == 5


def test_sessions_are_independent():
    r = WakeRegistry()
    r.on_start("s-a", 0, daemon_id=1)
    r.on_start("s-b", 0, daemon_id=1)
    assert r.claim_arm("s-a") == 0 and r.claim_arm("s-b") == 0
    r.on_status("s-a", 9)                 # A advances; B untouched
    assert r.claim_arm("s-a") == 9
    assert r.claim_arm("s-b") is None     # B still armed at 0 — answering/advancing A never skips B


def test_drop_removes_session():
    r = WakeRegistry()
    r.on_start("s-a", 0, daemon_id=1)
    r.claim_arm("s-a")
    r.drop("s-a")
    assert r.value("s-a") is None
    assert "s-a" not in r.active_sids()
    assert r.claim_arm("s-a") is None     # dropped -> nothing to arm


def test_new_daemon_clears_registry():
    r = WakeRegistry()
    r.on_start("s-a", 5, daemon_id=111)
    r.claim_arm("s-a")
    r.on_start("s-b", 0, daemon_id=222)   # NEW daemon (pid change): old sessions are gone
    assert r.value("s-a") is None         # cleared
    assert r.value("s-b") == 0
    assert r.claim_arm("s-b") == 0


def test_concurrent_claim_arms_exactly_once():
    r = WakeRegistry()
    r.on_start("s-a", 0, daemon_id=1)
    results = []
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()
        results.append(r.claim_arm("s-a"))

    ts = [threading.Thread(target=worker) for _ in range(20)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert sum(1 for x in results if x is not None) == 1   # exactly one waiter claimed
