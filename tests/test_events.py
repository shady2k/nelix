from daemon.events import EventQueue, RESPONDABLE_KINDS, CURSOR_EXPIRED


def _fixed_owner(mapping):
    """An owner_resolver that reads a fixed session_id->owner_id map (no disk). None for
    sessions not in the map (fail-closed, exactly like a missing owner.json)."""
    return lambda sid: mapping.get(sid)


# ---------------------------------------------------------------------------
# nelix-9a4.5: the event ring is bounded SEMANTIC state, not unbounded history.
# ---------------------------------------------------------------------------

def test_bounded_ring_evicts_oldest_history_under_load():
    # A flood of delivery/history events past the bound evicts the OLDEST first; the ring never
    # grows without limit. latest_seq still reports the last seq EVER published for the session
    # (cursor stays monotonic across eviction — never rewinds to a retained-only max).
    q = EventQueue(max_history=10, owner_floor=0, owner_resolver=_fixed_owner({"s1": "o"}))
    last = None
    for i in range(200):
        last = q.publish("s1", "x", "working", f"e{i}", "working")
    assert q.latest_seq("s1") == last.seq == 200
    assert q.latest_seq() == 200
    assert q.latest_seqs(["s1"]) == {"s1": 200}
    # A cursor that fell off the back of the ring gets the EXPLICIT resync signal — even though
    # NEWER events are still retained (concurrency-review fix: expiry is checked BEFORE delivery,
    # so a caller whose cursor was evicted is never silently handed a later event and left to
    # believe it never missed anything).
    assert q.latest_after(0, "s1") is CURSOR_EXPIRED
    # A caller still WITHIN the retained window (cursor >= the session's evicted-high) is delivered
    # the retained event, never spuriously expired. Arming at last-1 sits above every evicted seq.
    within = q.latest_after(last.seq - 1, "s1")
    assert within is not None and within is not CURSOR_EXPIRED and within.seq == last.seq


def test_unresolved_decision_is_pinned_through_a_flood_far_past_the_bound():
    # THE core invariant: an unresolved decision event is exempt from the eviction budget and can
    # NEVER be dropped while unresolved — even under a flood that dwarfs the bound.
    q = EventQueue(max_history=5, owner_floor=0, owner_resolver=_fixed_owner({"s1": "o", "s2": "o"}))
    dec = q.publish("s1", "x", "waiting_for_user", "answer me", "waiting_for_user", decision_id="d1")
    for i in range(1000):
        q.publish("s2", "x", "working", "", "working")
    # still pending, still discoverable, still DELIVERABLE to a waiter armed before it.
    assert q.pending("s1") is dec
    assert q.latest_after(0, "s1") is dec
    # resolving it lets it re-enter the normal (evictable) history budget.
    assert q.resolve_decision("d1", "answered") == dec.seq
    assert q.pending("s1") is None


def test_resolved_decision_re_enters_the_evictable_budget():
    q = EventQueue(max_history=2, owner_floor=0, owner_resolver=_fixed_owner({"s1": "o"}))
    d = q.publish("s1", "x", "waiting_for_user", "?", "waiting_for_user", decision_id="d1")
    for i in range(10):
        q.publish("s1", "x", "working", "", "working")
    assert q.pending("s1") is d                          # pinned: survived the first flood
    q.resolve_decision("d1", "answered")                 # now evictable
    for i in range(10):
        q.publish("s1", "x", "working", "", "working")
    assert q.pending("s1") is None
    # d fell off the back once it re-entered the evictable budget: arming at the OLD cursor 0 (which
    # predates d) now correctly resyncs rather than silently skipping to newer history.
    assert q.latest_after(0, "s1") is CURSOR_EXPIRED
    # a caller within the retained window still gets recent history, and it is not d.
    got = q.latest_after(q.latest_seq("s1") - 1, "s1")   # d is gone; only recent history remains
    assert got is not CURSOR_EXPIRED and got.event_id != d.event_id


def test_cursor_expired_signal_when_cursor_fell_off_the_back():
    # The silent-stall fix: a waiter armed at a cursor whose events were evicted (nothing newer
    # retained for its session) gets an EXPLICIT cursor_expired signal, never a silent None.
    q = EventQueue(max_history=5, owner_floor=0, owner_resolver=_fixed_owner({"s1": "o", "s2": "o"}))
    q.publish("s1", "x", "done", "old doorbell", "done_candidate")     # seq 1, s1's only event
    for i in range(20):
        q.publish("s2", "x", "working", "", "working")                 # flood evicts s1's seq 1
    # s1 has no retained events, and its cursor (0) is before an evicted seq -> resync signal.
    assert q.latest_after(0, "s1") is CURSOR_EXPIRED
    # wait_event returns it IMMEDIATELY (no blocking for the timeout).
    import time as _t
    t0 = _t.time()
    assert q.wait_event(after_seq=0, timeout=5, session_id="s1") is CURSOR_EXPIRED
    assert _t.time() - t0 < 1.0                                        # did not block the 5s


def test_resync_cursor_clears_expiry_no_hot_loop():
    # After cursor_expired, a caller re-/status's and re-arms at latest_seq(session). That cursor
    # must NOT re-trip cursor_expired (which would be an unthrottled resync hot loop).
    q = EventQueue(max_history=3, owner_floor=0, owner_resolver=_fixed_owner({"s1": "o"}))
    for i in range(20):
        q.publish("s1", "x", "working", "", "working")
    resync = q.latest_seq("s1")                            # what /status would hand back
    got = q.wait_event(after_seq=resync, timeout=0.1, session_id="s1")
    assert got is None                                     # nothing new, and NOT cursor_expired


def test_per_owner_floor_protects_a_quiet_owner_from_a_noisy_one():
    # A busy owner flooding events must not evict a quiet owner's unresolved doorbell OR its recent
    # delivery history. The doorbell is pinned (invariant 2); the floor additionally keeps the
    # quiet owner's recent DELIVERY history off the eviction budget under cross-owner pressure.
    owners = {"s-b": "B", "s-a": "A"}
    q = EventQueue(max_history=4, owner_floor=2, owner_resolver=_fixed_owner(owners))
    doorbell = q.publish("s-b", "x", "waiting_for_user", "?", "waiting_for_user", decision_id="db")
    b_hist = q.publish("s-b", "x", "done", "quiet delivery", "done_candidate")
    for i in range(100):
        q.publish("s-a", "x", "working", "", "working")   # noisy owner A floods
    assert q.pending("s-b") is doorbell                    # pinned decision survives
    assert q.latest_after(doorbell.seq, "s-b") is b_hist   # recent delivery history survives (floor)
    # A itself is bounded: its OLD history was evicted, so a cursor from before the eviction resyncs.
    assert q.latest_after(0, "s-a") is CURSOR_EXPIRED
    # but a cursor within A's retained window still gets its recent (only-retained) history.
    recent_a = q.latest_after(q.latest_seq("s-a") - 1, "s-a")
    assert recent_a is not None and recent_a is not CURSOR_EXPIRED and recent_a.seq > 50


def test_default_owner_resolver_buckets_by_disk_owner_record():
    # The default resolver reads the durable owner.json (daemon/owner.py), so cross-owner eviction
    # protection works off the SAME oracle every other route uses. Real disk, no injected fake.
    from conftest import own
    own("s-quiet01", "B")
    own("s-busy001", "A")
    q = EventQueue(max_history=3, owner_floor=1)           # default (real) resolver
    quiet = q.publish("s-quiet01", "x", "done", "b", "done_candidate")
    for i in range(50):
        q.publish("s-busy001", "x", "working", "", "working")
    assert q.latest_after(0, "s-quiet01") is quiet         # protected by its DISK owner's floor


# ---------------------------------------------------------------------------
# nelix-9a4.5 concurrency-review findings (adversarial pass).
# ---------------------------------------------------------------------------

def test_caught_up_cursor_never_spuriously_expires_across_eviction():
    # Finding #1 companion: expiry is now checked BEFORE delivery, but a caller that is CAUGHT UP
    # (cursor >= the session's evicted-high) must NEVER be spuriously expired — only a cursor that
    # genuinely fell off the back resyncs. Live delivery is unaffected.
    q = EventQueue(max_history=5, owner_floor=0, owner_resolver=_fixed_owner({"s1": "o"}))
    for i in range(50):
        q.publish("s1", "x", "working", "", "working")     # heavy eviction churn
    resync = q.latest_seq("s1")                             # a fully caught-up cursor
    assert q.latest_after(resync, "s1") is None             # nothing newer, NOT cursor_expired
    nxt = q.publish("s1", "x", "working", "next", "working")
    got = q.latest_after(resync, "s1")                      # the live next event is delivered
    assert got is nxt and got is not CURSOR_EXPIRED
    # and arming exactly AT the session's evicted-high (the boundary) delivers, never expires.
    assert q.latest_after(nxt.seq, "s1") is None            # caught up again -> plain None


def test_none_then_real_owner_transition_does_not_corrupt_buckets():
    # Finding #2: an event published while the owner is unresolved (None) is bucketed under None and
    # MUST stay there. A later cache flip to a real owner ("A", once owner.json becomes readable)
    # must not try to remove the None-bucketed event from A's bucket (ValueError in publish -> lost
    # wakeup) or drift _evictable_count. Force eviction of the None-bucketed event under a flood.
    calls = {"n": 0}

    def resolver(sid):
        calls["n"] += 1
        return None if calls["n"] == 1 else "A"            # first resolve None, then real owner "A"

    q = EventQueue(max_history=3, owner_floor=0, owner_resolver=resolver)
    first = q.publish("s1", "x", "working", "none-owner", "working")   # owner unresolved -> None
    assert first.owner is None
    last = None
    for i in range(20):
        last = q.publish("s1", "x", "working", "", "working")          # owner now resolves to "A"
    assert last.owner == "A"
    # No exception was raised by the flood's eviction of the None-bucketed 'first', and the
    # incrementally-maintained count still matches the actual per-owner history.
    assert q._evictable_count == sum(len(h) for h in q._owner_hist.values())
    assert first.event_id not in q._by_id                             # 'first' was evicted cleanly
    assert q.latest_seq("s1") == 21                                    # publish path stayed intact


def test_none_bucketed_event_evicts_and_still_wakes_a_waiter():
    # Finding #2 (wakeup half): the None->owner corruption used to raise inside publish BEFORE
    # notify_all(), so a blocked waiter was never woken. Verify a flood that evicts the None-bucketed
    # event still notifies a concurrently blocked waiter.
    import threading
    calls = {"n": 0}

    def resolver(sid):
        calls["n"] += 1
        return None if calls["n"] == 1 else "A"

    q = EventQueue(max_history=3, owner_floor=0, owner_resolver=resolver)
    q.publish("s1", "x", "working", "none-owner", "working")          # seq 1, None bucket
    woke = q.wait_event(after_seq=0, timeout=2, session_id="s1")      # sees seq 1 immediately
    assert woke is not None and woke.seq == 1
    got = {}
    started = threading.Event()

    def waiter():
        started.set()
        got["evt"] = q.wait_event(after_seq=1, timeout=2, session_id="s1")

    t = threading.Thread(target=waiter, daemon=True); t.start()
    assert started.wait(1)
    import time as _t; _t.sleep(0.05)
    for i in range(20):                                               # flood evicts the None event
        q.publish("s1", "x", "working", "", "working")
    t.join(2)
    assert not t.is_alive() and got.get("evt") is not None            # waiter was woken, not stuck


def test_owner_resolved_before_lock_not_under_it():
    # Finding #3: owner resolution reads owner.json from disk and MUST happen before self._cv is
    # taken (no filesystem I/O under the single lock guarding every publish/wait/status). Prove it:
    # while a resolver is (slowly) resolving, another thread can still acquire the queue.
    import threading
    entered = threading.Event()
    release = threading.Event()

    def slow_resolver(sid):
        entered.set()
        release.wait(2)                                    # block DURING resolution
        return "o"

    q = EventQueue(max_history=10, owner_floor=0, owner_resolver=slow_resolver)
    t = threading.Thread(
        target=lambda: q.publish("s1", "x", "working", "", "working"), daemon=True)
    t.start()
    assert entered.wait(2)                                 # resolver is mid-flight
    # If resolution were under _cv this would deadlock until release; it must return promptly.
    assert q.latest_seq("s1") == 0                         # nothing published yet (still resolving)
    release.set()
    t.join(2)
    assert not t.is_alive() and q.latest_seq("s1") == 1


def test_eviction_skips_a_not_yet_indexed_event():
    # Fix pass 3 (A): during a nested publish inside on_publish, the OUTER event is already in
    # _events/_by_id but NOT yet in its owner's _owner_hist bucket — its _index_new runs only in
    # publish()'s finally. The nested publish's eviction scans _events and MUST NEVER select that
    # un-indexed outer event: doing so crashes _drop's hist.remove with ValueError (and leaves the
    # ring half-mutated). Everything published before the outer event here is a PINNED decision, so
    # the ascending scan reaches the un-indexed outer first — the membership guard makes it skip past
    # to the real (indexed) victim. No exception, count stays exact.
    q = EventQueue(max_history=0, owner_floor=0, owner_resolver=_fixed_owner({"s": "o"}))
    pin = q.publish("s", "x", "waiting_for_user", "pin", "waiting_for_user", decision_id="d1")

    def hook(outer):
        # a nested publish for the SAME owner drives the evictable count over the (0) bound while
        # `outer` is still un-indexed; the nested _evict_if_needed must not pick `outer`.
        q.publish("s", "x", "working", "nested", "working")

    # WITHOUT the guard this raises ValueError inside the nested eviction; WITH it, publish returns.
    e = q.publish("s", "x", "working", "outer", "working", on_publish=hook)
    assert e.summary == "outer"
    assert q.pending("s") is pin                                     # pinned decision untouched
    assert q._evictable_count == sum(len(h) for h in q._owner_hist.values())


def test_raising_on_publish_leaves_the_queue_consistent_and_notifies():
    # Fix pass 3 (B + D): an on_publish hook that raises must (1) leave the event FULLY indexed and
    # the queue internally consistent (never half-indexed), watermarks monotonic (never rewound), and
    # (2) notify a blocked waiter ON THE EXCEPTION PATH — PROMPTLY, not only after its full timeout.
    # A NON-nested raising hook is used deliberately (D): the ONLY notify_all that can wake the waiter
    # is the outer publish's own finally, so a nested publish's finally cannot mask a dropped
    # exception-path notify. The waiter is armed before the (raising) event and returns exactly it.
    import threading, pytest, time as _t
    q = EventQueue(max_history=100, owner_floor=0, owner_resolver=_fixed_owner({"s1": "o"}))
    got = {}
    started = threading.Event()

    def waiter():
        started.set()
        got["t0"] = _t.time()
        got["evt"] = q.wait_event(after_seq=0, timeout=5, session_id="s1")
        got["t1"] = _t.time()

    t = threading.Thread(target=waiter, daemon=True); t.start()
    assert started.wait(1)
    _t.sleep(0.1)                                          # let the waiter reach _cv.wait

    def boom(ev):
        raise RuntimeError("on_publish blew up (no nesting)")

    with pytest.raises(RuntimeError):
        q.publish("s1", "x", "working", "outer", "working", on_publish=boom)

    # (a) the event is FULLY indexed (not rolled back), count consistent, watermark monotonic.
    assert [e.summary for e in q._events if e.session_id == "s1"] == ["outer"]
    assert q._evictable_count == sum(len(h) for h in q._owner_hist.values()) == 1
    assert q.latest_seq() == 1 and q.latest_seq("s1") == 1
    assert q.latest_after(0, "s1").summary == "outer"     # deliverable to a caught-up-behind cursor
    # (b) the blocked waiter was notified on the EXCEPTION path and returned the event PROMPTLY (well
    # under its 5s timeout). A dropped finally: notify_all would only wake it at timeout -> this FAILS.
    t.join(3)
    assert not t.is_alive()
    assert got["evt"] is not None and got["evt"].summary == "outer"
    assert got["t1"] - got["t0"] < 3


def test_nested_publish_in_on_publish_stays_indexed_and_advances_watermark():
    # Fix pass 3 (B, test b): an on_publish that does a NESTED publish then raises — the nested event
    # stays indexed and BOTH watermarks reflect the nested seq (never rewound below it). The outer
    # event is ALSO fully retained (B removes the outer-rollback), no ValueError, count consistent.
    import pytest
    q = EventQueue(max_history=100, owner_floor=0, owner_resolver=_fixed_owner({"s1": "o"}))
    captured = {}

    def hook(outer):
        captured["nested"] = q.publish("s1", "x", "working", "nested", "working")
        raise RuntimeError("outer hook blew up after a nested publish")

    with pytest.raises(RuntimeError):
        q.publish("s1", "x", "working", "outer", "working", on_publish=hook)

    nested = captured["nested"]
    assert q._by_id.get(nested.event_id) is nested                  # nested retained + indexed
    # BOTH the outer AND the nested are retained (no outer-rollback), count consistent, no crash.
    assert sorted(e.summary for e in q._events if e.session_id == "s1") == ["nested", "outer"]
    assert q._evictable_count == sum(len(h) for h in q._owner_hist.values()) == 2
    # watermarks reflect the NESTED (highest) seq and were never rewound.
    assert q.latest_seq() == nested.seq
    assert q.latest_seq("s1") == nested.seq


def test_raising_publish_does_not_rewind_watermark_below_evicted_high():
    # Fix pass 3 (B, test c): with max_history=0 every evictable event is evicted immediately. A first
    # publish is evicted (evicted_high=1); a subsequent RAISING publish must NOT rewind latest_seq
    # below evicted_high. The reverted rollback recomputed _last_seq from RETAINED events — which,
    # after an eviction, rewinds it below _evicted_high and makes /status's resync cursor report
    # CURSOR_EXPIRED forever (and can false-expire a live session).
    import pytest
    q = EventQueue(max_history=0, owner_floor=0, owner_resolver=_fixed_owner({"s1": "o"}))
    q.publish("s1", "x", "working", "e1", "working")                # seq 1, immediately evicted
    assert q._evicted_high_by_session.get("s1") == 1

    def boom(ev):
        raise RuntimeError("hook blew up")

    with pytest.raises(RuntimeError):
        q.publish("s1", "x", "working", "e2", "working", on_publish=boom)   # seq 2
    # latest_seq stays >= evicted_high (monotonic, never rewound below it).
    assert q.latest_seq("s1") == 2 >= q._evicted_high_by_session.get("s1")
    # the resync cursor clears expiry — no permanent CURSOR_EXPIRED / resync hot loop.
    assert q.latest_after(q.latest_seq("s1"), "s1") is None


def test_forget_session_releases_retention_and_bookkeeping():
    # Finding #4: forget_session drops a definitively-removed session's retained events AND all its
    # per-session bookkeeping, so a long-lived daemon's ring is bounded to live + not-yet-pruned
    # sessions rather than leaking one entry per distinct session EVER published.
    q = EventQueue(max_history=5, owner_floor=0, owner_resolver=_fixed_owner({"s1": "o", "s2": "o"}))
    for sid in ("s1", "s2"):
        q.publish(sid, "x", "waiting_for_user", "?", "waiting_for_user", decision_id=f"d-{sid}")
        for i in range(30):
            q.publish(sid, "x", "working", "", "working")
    assert q.latest_seq("s1") > 0 and "s1" in q._last_seq_by_session
    q.forget_session("s1")
    # s1 is gone from every structure...
    assert not any(e.session_id == "s1" for e in q._events)
    assert all(e.session_id != "s1" for e in q._by_id.values())
    assert "s1" not in q._last_seq_by_session
    assert "s1" not in q._evicted_high_by_session
    assert "s1" not in q._owner_cache
    assert q.latest_seq("s1") == 0
    assert q.pending("s1") is None
    # ...and the count invariant still holds, backed only by s2's retained events.
    assert q._evictable_count == sum(len(h) for h in q._owner_hist.values())
    assert q.pending("s2") is not None                     # s2 fully intact
    # idempotent: forgetting an unknown / already-forgotten session is a harmless no-op.
    q.forget_session("s1")
    q.forget_session("never-existed")


def test_forget_session_does_not_drop_another_sessions_pinned_or_history():
    # Finding #4 (targeted): forgetting one session must not disturb another session's pinned
    # decision or its recent history.
    q = EventQueue(max_history=50, owner_floor=0, owner_resolver=_fixed_owner({"keep": "o", "drop": "o"}))
    dec = q.publish("keep", "x", "waiting_for_user", "?", "waiting_for_user", decision_id="d-keep")
    hist = q.publish("keep", "x", "done", "recent", "done_candidate")
    q.publish("drop", "x", "waiting_for_user", "?", "waiting_for_user", decision_id="d-drop")
    q.forget_session("drop")
    assert q.pending("keep") is dec                        # keep's decision still pinned
    assert q.latest_after(dec.seq, "keep") is hist         # keep's recent history still deliverable
    assert q.pending("drop") is None
    assert "d-drop" not in q._by_decision
    assert q._evictable_count == sum(len(h) for h in q._owner_hist.values())


def test_global_seq_across_sessions():
    q = EventQueue()
    a = q.publish("s1", "claude", "waiting_for_user", "a", "waiting_for_user")
    b = q.publish("s2", "codex", "done", "b", "done_candidate")
    assert (a.seq, b.seq) == (1, 2)
    assert a.session_id == "s1" and b.executor == "codex"
    assert q.latest_after(1).seq == 2 and q.latest_after(2) is None


def test_pending_filtered_by_session():
    q = EventQueue()
    e1 = q.publish("s1", "claude", "waiting_for_user", "y/n?", "waiting_for_user")
    q.publish("s2", "codex", "waiting_for_user", "y/n?", "waiting_for_user")
    assert q.pending("s1").event_id == e1.event_id
    assert q.mark_answered(e1.event_id) == e1.seq        # returns the answered seq, not bool
    assert q.pending("s1") is None
    assert q.pending("s2") is not None


def test_mark_answered_returns_seq():
    q = EventQueue()
    e = q.publish("s-1", "a", "waiting_for_user", "?", "idle_prompt")
    assert q.mark_answered(e.event_id) == e.seq
    assert q.mark_answered("evt-nope") is None


def test_latest_seq_and_session_scoped_wait():
    q = EventQueue()
    assert q.latest_seq() == 0
    a = q.publish("s-A", "x", "blocked", "", "startup_interstitial")
    q.publish("s-B", "x", "blocked", "", "startup_interstitial")
    assert q.latest_seq() == 2
    # session-scoped: from before A, only s-A's event is returned for session s-A
    assert q.latest_after(0, session_id="s-A") is a
    assert q.latest_after(a.seq, session_id="s-A") is None


def test_no_stale_wake_after_respond():
    # The cursor model: after answering A, arming past A.seq must NOT re-return A — only the
    # NEXT event (B) wakes the orchestrator. This is what kills the stale-wake loop.
    q = EventQueue()
    a = q.publish("s1", "claude", "waiting_for_user", "?", "idle_prompt")
    assert q.mark_answered(a.event_id) == a.seq
    assert q.wait_event(after_seq=a.seq, timeout=0.1, session_id="s1") is None
    b = q.publish("s1", "claude", "waiting_for_user", "?2", "idle_prompt")
    assert q.wait_event(after_seq=a.seq, timeout=0.1, session_id="s1") is b


def test_wait_event_blocks_then_returns():
    import threading, time
    q = EventQueue()
    def producer():
        time.sleep(0.05); q.publish("s1", "claude", "done", "x", "done_candidate")
    threading.Thread(target=producer, daemon=True).start()
    assert q.wait_event(after_seq=0, timeout=2, session_id="s1").seq == 1
    assert q.wait_event(after_seq=1, timeout=0.1, session_id="s1") is None


def test_wait_event_requires_session_id():
    # session_id is a REQUIRED parameter: the wait primitive must be STRUCTURALLY incapable of a
    # global (cross-session) wait, which would deliver one session's event to another's orchestrator.
    # Omitting it is a programming error (TypeError), never a silent global wait.
    import pytest
    q = EventQueue()
    with pytest.raises(TypeError):
        q.wait_event(after_seq=0, timeout=0.1)


def test_wait_event_rejects_explicit_none_or_empty_session_id():
    # Dropping the default catches OMISSION (TypeError); a top-of-function guard also refuses an
    # EXPLICIT None or empty session_id (ValueError). Together there is NO session_id=None variant at
    # all — the primitive is structurally incapable of a global wait, not merely by convention.
    import pytest
    q = EventQueue()
    with pytest.raises(ValueError):
        q.wait_event(after_seq=0, timeout=0.1, session_id=None)
    with pytest.raises(ValueError):
        q.wait_event(after_seq=0, timeout=0.1, session_id="")


# ---------------------------------------------------------------------------
# nelix-3rm 3c.3b: the MULTI-SESSION (session-set) wait primitive — one waiter for an
# orchestration's N sessions. Wakes on the next event of ANY session in a SET, keeping every
# 9a4.5 ring invariant intact (single self._cv, notify_all from publish only, no new lock,
# pinned decisions / per-owner floor / monotonic watermarks untouched, expiry BEFORE delivery,
# no empty/global wait).
# ---------------------------------------------------------------------------

def test_wait_event_any_wakes_on_an_event_of_any_session_in_the_set():
    q = EventQueue(owner_resolver=_fixed_owner({"s1": "o", "s2": "o", "s3": "o"}))
    a = q.publish("s2", "x", "waiting_for_user", "hi", "waiting_for_user")   # seq 1, a MEMBER
    got = q.wait_event_any(after_seq=0, timeout=1, session_ids={"s1", "s2"})
    assert got is a                                          # the member's event, returned by identity
    assert got.session_id == "s2" and got.seq == 1          # tells the caller WHICH session + where


def test_wait_event_any_ignores_events_outside_the_set():
    # An event on a session NOT in the set must never wake a set waiter (that is the whole owner-
    # isolation point: another orchestration's event must not be delivered here).
    q = EventQueue(owner_resolver=_fixed_owner({"s1": "o", "s2": "o", "s-out": "o"}))
    q.publish("s-out", "x", "working", "not mine", "working")               # seq 1, OUT of the set
    assert q.wait_event_any(after_seq=0, timeout=0.1, session_ids={"s1", "s2"}) is None
    # then a member publishes: NOW it wakes, and returns the member's (later) event, not the outsider.
    b = q.publish("s1", "x", "waiting_for_user", "mine", "waiting_for_user")  # seq 2
    assert q.wait_event_any(after_seq=0, timeout=1, session_ids={"s1", "s2"}) is b


def test_wait_event_any_rejects_an_empty_set():
    # An empty set matches EVERY session (a global wait), which would deliver another owner's event
    # here — refused exactly like wait_event's missing-session_id guard. Structurally incapable of a
    # global wait, not merely by convention.
    import pytest
    q = EventQueue()
    with pytest.raises(ValueError):
        q.wait_event_any(after_seq=0, timeout=0.1, session_ids=set())
    with pytest.raises(ValueError):
        q.wait_event_any(after_seq=0, timeout=0.1, session_ids=[])


def test_wait_event_any_returns_cursor_expired_when_a_members_cursor_fell_off_the_ring():
    # If the after_seq fell off the back for ANY session in the set (its evicted-high), the caller
    # missed an event it cares about -> the explicit resync marker, immediately (no blocking).
    import time as _t
    q = EventQueue(max_history=5, owner_floor=0,
                   owner_resolver=_fixed_owner({"s1": "o", "s2": "o", "flood": "o"}))
    q.publish("s1", "x", "done", "old doorbell", "done_candidate")          # seq 1, s1's only event
    for i in range(30):
        q.publish("flood", "x", "working", "", "working")                  # evicts s1's seq 1
    t0 = _t.time()
    assert q.wait_event_any(after_seq=0, timeout=5, session_ids={"s1", "s2"}) is CURSOR_EXPIRED
    assert _t.time() - t0 < 1.0                                            # returned at once, no block


def test_wait_event_any_caught_up_member_does_not_spuriously_expire():
    # A set whose after_seq is >= every member's evicted-high must NOT expire — only a genuine gap
    # resyncs. Arming at the global latest_seq (a fully caught-up cursor) is the resync cursor and
    # must clear expiry (no resync hot loop), exactly as the single-session path guarantees.
    q = EventQueue(max_history=5, owner_floor=0,
                   owner_resolver=_fixed_owner({"s1": "o", "s2": "o"}))
    for i in range(40):
        q.publish("s1", "x", "working", "", "working")                     # heavy churn
    resync = q.latest_seq()                                                # fully caught-up
    assert q.wait_event_any(after_seq=resync, timeout=0.1, session_ids={"s1", "s2"}) is None


def test_wait_event_any_wakes_a_blocked_waiter_when_a_member_publishes():
    # THE concurrency invariant: a waiter blocked on a SET wakes when ONE member publishes — via
    # publish()'s own notify_all() (the multi-session wait adds no notify of its own).
    import threading, time as _t
    q = EventQueue(owner_resolver=_fixed_owner({"s1": "o", "s2": "o"}))
    got = {}
    started = threading.Event()

    def waiter():
        started.set()
        got["evt"] = q.wait_event_any(after_seq=0, timeout=3, session_ids={"s1", "s2"})

    t = threading.Thread(target=waiter, daemon=True); t.start()
    assert started.wait(1)
    _t.sleep(0.1)                                                          # let it reach _cv.wait
    b = q.publish("s2", "x", "waiting_for_user", "wake up", "waiting_for_user")
    t.join(3)
    assert not t.is_alive()
    assert got["evt"] is b                                                 # woken by the member, not stuck


def test_wait_event_any_single_member_set_matches_the_single_session_wait():
    # A one-element set is behaviorally identical to the single-session wait (the router's N=1
    # orchestration collapses to it) — same event, same None-on-timeout.
    q = EventQueue(owner_resolver=_fixed_owner({"s1": "o", "s2": "o"}))
    q.publish("s2", "x", "working", "other", "working")                    # seq 1, not a member
    assert q.wait_event_any(after_seq=0, timeout=0.1, session_ids={"s1"}) is None
    a = q.publish("s1", "x", "waiting_for_user", "mine", "waiting_for_user")  # seq 2
    assert q.wait_event_any(after_seq=0, timeout=1, session_ids={"s1"}) is a


def test_single_session_wait_unchanged_after_generalizing_first_after():
    # The single-session path (str filter) and the global query (None filter) still work after
    # _first_after/_expired were generalized to accept a set — the existing 9a4.5 callers rely on it.
    q = EventQueue(owner_resolver=_fixed_owner({"s1": "o", "s2": "o"}))
    a = q.publish("s1", "x", "waiting_for_user", "a", "waiting_for_user")
    b = q.publish("s2", "x", "done", "b", "done_candidate")
    assert q.wait_event(after_seq=0, timeout=0.1, session_id="s1") is a    # str filter, unchanged
    assert q.latest_after(0) is a and q.latest_after(1).seq == b.seq       # None filter, unchanged


def test_publish_carries_range_and_hint_under_lock():
    from daemon.events import EventQueue
    q = EventQueue()
    e = q.publish("s1", "demo", "waiting_for_user", "sum", "idle_prompt",
                  turn_index=2, range=(5, 9), hint="needs_permission", hung=False)
    assert e.turn_index == 2 and e.range == (5, 9) and e.hint == "needs_permission"
    pend = q.pending()
    assert pend is not None and pend.event_id == e.event_id
    q.mark_answered(e.event_id)
    assert q.pending() is None


def test_resolve_decision_clears_all_reemits_of_that_decision():
    # A pause can span several events (a re-emit / nag) sharing one decision_id. Resolving the
    # decision (by id) must clear ALL of them — targeted, not a blanket session-answer (IMPORTANT-7).
    q = EventQueue()
    q.publish("s1", "x", "blocked", "trust?", "startup_interstitial",
              requires_response=True, decision_id="dec-A")
    b = q.publish("s1", "x", "blocked", "trust?", "startup_interstitial",
                  requires_response=True, hung=True, decision_id="dec-A")
    # a DIFFERENT decision for the same session must NOT be resolved
    other = q.publish("s1", "x", "waiting_for_user", "?", "idle_prompt",
                      requires_response=True, decision_id="dec-B")
    q.publish("s2", "x", "waiting_for_user", "?", "idle_prompt",
              requires_response=True, decision_id="dec-C")
    assert q.resolve_decision("dec-A", "answered") == b.seq    # highest seq of that decision
    assert q.pending("s1") is other                            # dec-B still pending (not resolved)
    assert q.pending("s2") is not None                         # other session untouched
    assert q.resolve_decision("dec-A", "answered") is None     # nothing left to resolve for dec-A


def test_resolve_decision_records_the_reason():
    q = EventQueue()
    e = q.publish("s1", "x", "waiting_for_user", "?", "idle_prompt",
                  requires_response=True, decision_id="dec-Z")
    assert e.resolved_reason is None
    q.resolve_decision("dec-Z", "withdrawn")
    assert e.resolved_reason == "withdrawn"
    assert q.pending("s1") is None                             # withdrawn -> not pending


def test_publish_on_publish_runs_before_waiters_are_notified():
    # Install-before-notify: the on_publish hook (where the session installs its decision) runs
    # while the event is reserved but BEFORE waiters wake — so a woken status pull never observes
    # an event without its decision.
    import threading, time as _t
    q = EventQueue()
    order = []
    started = threading.Event()

    def waiter():
        started.set()
        evt = q.wait_event(after_seq=0, timeout=2, session_id="s1")
        order.append(("woke", evt.event_id if evt else None))

    t = threading.Thread(target=waiter, daemon=True); t.start()
    assert started.wait(1)
    _t.sleep(0.05)                                      # let the waiter block on the condition
    e = q.publish("s1", "x", "waiting_for_user", "?", "idle_prompt",
                  on_publish=lambda ev: order.append(("installed", ev.event_id)))
    t.join(2)
    assert order[0] == ("installed", e.event_id)        # install BEFORE the waiter wakes
    assert order[1] == ("woke", e.event_id)


def test_latest_seq_global_and_per_session():
    q = EventQueue()
    a = q.publish("s-a", "ex", "working", "", "working")
    b = q.publish("s-b", "ex", "working", "", "working")
    c = q.publish("s-a", "ex", "waiting_for_user", "", "working")
    assert q.latest_seq() == c.seq          # global unchanged
    assert q.latest_seq("s-a") == c.seq
    assert q.latest_seq("s-b") == b.seq
    assert q.latest_seq("s-missing") == 0


def test_latest_seqs_single_pass():
    q = EventQueue()
    q.publish("s-a", "ex", "working", "", "working")
    b = q.publish("s-b", "ex", "working", "", "working")
    c = q.publish("s-a", "ex", "blocked", "", "working")
    assert q.latest_seqs(["s-a", "s-b", "s-x"]) == {"s-a": c.seq, "s-b": b.seq, "s-x": 0}


def test_blocked_event_is_pending_and_carries_fields():
    q = EventQueue()
    e = q.publish("s-1", "agent", "blocked", "trust?", "startup_interstitial",
                  hint="task_not_delivered", task_delivery="pending",
                  requires_response=True, screen_excerpt="❯ 1. Yes")
    assert e.task_delivery == "pending" and e.requires_response is True
    assert e.screen_excerpt == "❯ 1. Yes"
    assert q.pending("s-1") is e                 # blocked is respondable
    q.mark_answered(e.event_id)
    assert q.pending("s-1") is None
    assert "blocked" in RESPONDABLE_KINDS and "waiting_for_user" in RESPONDABLE_KINDS
