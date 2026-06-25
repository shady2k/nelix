from daemon.events import EventQueue, RESPONDABLE_KINDS


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
    assert q.wait_event(after_seq=0, timeout=2).seq == 1
    assert q.wait_event(after_seq=1, timeout=0.1) is None


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
